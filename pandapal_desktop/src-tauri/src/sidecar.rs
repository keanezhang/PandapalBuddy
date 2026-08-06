//! pandapal-desktop/src-tauri/src/sidecar.rs
//!
//! 职责：
//! - 通过 std::process::Command 启动 Python sidecar 进程（onedir 模式，单进程）
//!   串行启动架构：登录成功后由 Rust 携带 --user-id / --token 参数启动
//! - 异步读取 stdout：
//!   - 解析 "PANDAPAL_READY" 信号 → emit "backend-ready"
//!   - 解析 "IPC:{json}" 前缀行 → emit "backend-event" 事件到前端
//! - 提供 stdin 写入接口（前端 invoke → Rust write stdin → Python）
//! - 进程退出后向前端 emit "backend-crashed" 事件（主动 kill 时静默）
//!
//! 改造要点（相对于旧版）：
//! - 不再使用 tauri-plugin-shell sidecar，改用 std::process::Command 直接启动
//! - onedir 模式下只有 1 个进程，kill 逻辑大幅简化
//! - 移除 pkill、移除固定 sleep(500ms)

use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, Runtime};
use serde::Serialize;

/// Sidecar 进程状态
pub struct SidecarState {
    /// std::process::Child 句柄
    pub child: Option<Child>,
    /// stdin 写入句柄（从 child 中 take 出来单独管理）
    pub stdin_handle: Option<std::process::ChildStdin>,
    /// kill_flag: shutdown 时置 true，告知 stdout 读取线程本次终止是预期的
    pub kill_flag: Arc<AtomicBool>,
    /// 当前登录用户的 ID
    pub user_id: String,
    /// 当前会话 ID（格式 {user_id}_session_{uuidv4}，首次 send_message 时惰性生成）
    pub session_id: String,
    /// 当前进程服务的工作区根目录（--workdir）。空表示尚未打开工作区。
    /// 一进程一目录：切换工作区需 kill 本进程后用新 workdir 重启。
    pub workspace: String,
}

impl SidecarState {
    pub fn new() -> Self {
        Self {
            child: None,
            stdin_handle: None,
            kill_flag: Arc::new(AtomicBool::new(false)),
            user_id: String::new(),
            session_id: String::new(),
            workspace: String::new(),
        }
    }
}

/// 已解析到的后端鉴权 Token（空字符串表示尚未就绪）
#[derive(Default)]
pub struct BackendToken(pub Arc<Mutex<String>>);

/// 后端当前激活的模型信息 (model_id, provider)；从 PANDAPAL_READY 握手解析而来。
/// 供 sidecar 热重启时重新 emit backend-ready 携带模型信息。
#[derive(Default)]
pub struct BackendModel(pub Arc<Mutex<(String, String)>>);

/// backend-ready 事件 payload（发给前端）
#[derive(Serialize, Clone)]
pub struct BackendReadyPayload {
    pub token: String,
    /// 后端激活的模型 id（空字符串表示未知）
    pub model: String,
    /// 后端激活的 provider（dashscope / volcengine / openai / deepseek）
    pub provider: String,
}

/// 向 sidecar stdin 写入一行（前端 → Rust → Python stdin）
///
/// 发送 JSON + 换行符作为一条完整指令。
pub fn write_to_sidecar<R: Runtime>(app: &AppHandle<R>, message: &str) -> Result<(), String> {
    let state = app.state::<Mutex<SidecarState>>();
    let mut s = state.lock().map_err(|e| format!("Lock error: {}", e))?;

    if let Some(ref mut stdin) = s.stdin_handle {
        let data = format!("{}\n", message);
        stdin
            .write_all(data.as_bytes())
            .map_err(|e| format!("Write to sidecar stdin failed: {}", e))?;
        stdin
            .flush()
            .map_err(|e| format!("Flush sidecar stdin failed: {}", e))?;
        Ok(())
    } else {
        Err("Sidecar not running".to_string())
    }
}

/// 获取 sidecar 可执行文件路径（跨平台 + 开发/生产模式自动适配）
///
/// 开发模式 (debug_assertions)：优先从 CARGO_MANIFEST_DIR/bin/ 加载，始终是最新构建
/// 生产模式：从 Tauri resource_dir 加载（app bundle 内嵌）
fn get_sidecar_exe_path<R: Runtime>(app: &AppHandle<R>) -> Result<std::path::PathBuf, String> {
    let triple = current_target_triple();

    let exe_name = get_exe_name(&triple);
    let sidecar_dir_name = format!("pandapal-sidecar-{}", triple);

    // 开发模式：直接从 build_sidecar 输出目录加载，不依赖 resource_dir 的 stale 缓存
    if cfg!(debug_assertions) {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let dev_exe = manifest_dir
            .join("bin")
            .join(&sidecar_dir_name)
            .join(&exe_name);
        if dev_exe.exists() {
            return Ok(dev_exe);
        }
    }

    // 生产模式 / 开发兜底：从 Tauri resource_dir 加载
    if let Ok(resource_dir) = app.path().resource_dir() {
        let path1 = resource_dir.join("bin").join(&sidecar_dir_name).join(&exe_name);
        if path1.exists() {
            return Ok(path1);
        }
        let path2 = resource_dir.join(&sidecar_dir_name).join(&exe_name);
        if path2.exists() {
            return Ok(path2);
        }
    }

    // 生产模式兜底：尝试编译时 manifest_dir/bin （跨机器构建时 resource_dir 可能不包含 sidecar）
    {
        let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let fallback_exe = manifest_dir
            .join("bin")
            .join(&sidecar_dir_name)
            .join(&exe_name);
        if fallback_exe.exists() {
            return Ok(fallback_exe);
        }
    }

    Err(format!(
        "Sidecar binary not found for target '{}'. Run build_sidecar script first.",
        triple
    ))
}

/// 获取当前编译目标的 target triple
fn current_target_triple() -> &'static str {
    env!("TARGET_TRIPLE")
}

/// 根据平台获取可执行文件名
fn get_exe_name(triple: &str) -> String {
    if triple.contains("windows") {
        format!("pandapal-sidecar-{}.exe", triple)
    } else {
        format!("pandapal-sidecar-{}", triple)
    }
}

/// 启动 Python sidecar，传入 user_id + token 作为 CLI 参数。
///
/// 串行启动架构：登录成功后由 Rust 携带 --user-id / --token 启动，
/// Python 在构建阶段即可初始化完整的 memory backends。
///
/// **幂等性保证**：若 sidecar 已在运行且 BackendToken 非空（已就绪），则重新
/// emit "backend-ready" 事件，直接返回，不重启进程。
pub fn spawn_sidecar<R: Runtime>(app: &AppHandle<R>, user_id: &str, token: &str, workdir: &str, app_data_dir: &str) {
    // ★ 原子临界区：「检查是否已运行」+「spawn」+「登记 child」必须在同一把锁内完成。
    //   旧实现把「检查」与「登记」拆到两把锁、中间夹着阻塞的 command.spawn()，两次并发调用
    //   会都通过 is_some() 检查、各起一个进程（TOCTOU 双开，后写者覆盖前者句柄→前者泄漏）。
    //   全程持锁根除该竞态；spawn() 仅数毫秒，短暂阻塞其他 SidecarState 访问可接受。
    let stdout;
    let stderr;
    let task_kill_flag;
    {
        let state = app.state::<Mutex<SidecarState>>();
        let mut s = state.lock().unwrap();
        s.user_id = user_id.to_string();

        // 已在运行：重新 emit backend-ready 即可，绝不重启
        //
        // ⚠️ 这里**不得**用「token 非空」当作就绪判据：PANDAPAL_READY 握手根本不带
        //    token（run_local.py 只写 model=/provider=），BackendToken 恒为空串。
        //    旧代码的 `if !current_token.is_empty()` 因此永不成立 → 热重启时
        //    start_sidecar 静默返回、backend-ready 永不重发 → 前端 CredentialGate
        //    卡在「正在启动」且无超时无报错。就绪的唯一真相源是 child.is_some()。
        if s.child.is_some() {
            let current_token = app.state::<BackendToken>().0.lock().unwrap().clone();
            let (model, provider) = app.state::<BackendModel>().0.lock().unwrap().clone();
            let payload = BackendReadyPayload { token: current_token, model, provider };
            let _ = app.emit("backend-ready", payload);
            return;
        }

        let exe_path = match get_sidecar_exe_path(app) {
            Ok(p) => p,
            Err(e) => {
                eprintln!("[sidecar] ERROR: {}", e);
                let _ = app.emit("backend-crashed", e);
                return;
            }
        };

        eprintln!("[sidecar] launching: {:?}", exe_path);

        let mut command = Command::new(&exe_path);
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        // 传 --user-id / --token / --workdir
        // --workdir 是用户「打开文件夹」选定的工作区根，Agent 文件工具唯一的根目录。
        // sidecar 自己会根据环境自动选择「应用数据根」（.env / SQLite / .pandapal）。
        command.args([
            "--user-id", user_id,
            "--token", token,
            "--workdir", workdir,
            "--app-data-dir", app_data_dir,
        ]);

        // Windows: 隐藏 Console 窗口但保留 stdin/stdout
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x08000000;
            command.creation_flags(CREATE_NO_WINDOW);
        }

        let mut child = match command.spawn() {
            Ok(c) => c,
            Err(e) => {
                let msg = format!("Failed to spawn sidecar: {}", e);
                eprintln!("[sidecar] {}", msg);
                let _ = app.emit("backend-crashed", msg);
                return;
            }
        };

        let pid = child.id();
        eprintln!("[sidecar] spawned (pid={})", pid);

        // ★ Windows：把子进程绑定到 KILL_ON_JOB_CLOSE 的 Job Object。
        //   Tauri 进程无论优雅退出、崩溃、还是被 cargo/dev 强杀，OS 关闭 Job 句柄即连带
        //   杀死 sidecar，根除「父死子留」的孤儿进程（dev 每次重启攒一个的元凶）。
        //   失败仅告警不阻断启动——退化为「无孤儿防护」，不影响正常功能。
        #[cfg(windows)]
        assign_to_kill_on_close_job(&child);

        // 取出 stdin/stdout/stderr 句柄
        let stdin_handle = child.stdin.take();
        stdout = child.stdout.take();
        stderr = child.stderr.take();

        // 创建新的 kill_flag
        let kill_flag = Arc::new(AtomicBool::new(false));
        task_kill_flag = kill_flag.clone();

        // 登记 child + stdin 到 State（仍在同一把锁内）
        s.child = Some(child);
        s.stdin_handle = stdin_handle;
        s.kill_flag = kill_flag;
        s.workspace = workdir.to_string();
    }

    // 异步读取 stdout（在独立线程中，通过 channel 传回 Tauri async runtime）
    let app_handle = app.clone();
    if let Some(stdout) = stdout {
        let stdout_kill_flag = task_kill_flag.clone();
        let stdout_app = app_handle.clone();
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line_result in reader.lines() {
                match line_result {
                    Ok(line) => {
                        let trimmed = line.trim().to_string();
                        if trimmed.is_empty() {
                            continue;
                        }

                        // IPC 消息
                        if let Some(json_str) = trimmed.strip_prefix("IPC:") {
                            let _ = stdout_app.emit("backend-event", json_str.to_string());
                        }
                        // READY 信号
                        else if let Some(info) = parse_ready_signal(&trimmed) {
                            let token_state = stdout_app.state::<BackendToken>();
                            *token_state.0.lock().unwrap() = info.token.clone();

                            let model_state = stdout_app.state::<BackendModel>();
                            *model_state.0.lock().unwrap() =
                                (info.model.clone(), info.provider.clone());

                            let payload = BackendReadyPayload {
                                token: info.token,
                                model: info.model,
                                provider: info.provider,
                            };
                            let _ = stdout_app.emit("backend-ready", payload);
                            log_to_console(&stdout_app, "[sidecar] backend ready (IPC mode)");
                        }
                        // 其他 stdout
                        else {
                            log_to_console(
                                &stdout_app,
                                &format!("[sidecar stdout] {}", trimmed),
                            );
                        }
                    }
                    Err(e) => {
                        if !stdout_kill_flag.load(Ordering::Acquire) {
                            log_to_console(
                                &stdout_app,
                                &format!("[sidecar stdout error] {}", e),
                            );
                        }
                        break;
                    }
                }
            }

            // stdout 结束 → 进程已退出
            if !stdout_kill_flag.load(Ordering::Acquire) {
                let msg = "[sidecar] process terminated unexpectedly".to_string();
                log_to_console(&stdout_app, &msg);
                let _ = stdout_app.emit("backend-crashed", msg);
            }
        });
    }

    // 异步读取 stderr（独立线程）
    if let Some(stderr) = stderr {
        let stderr_app = app_handle.clone();
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line_result in reader.lines() {
                match line_result {
                    Ok(line) => {
                        let trimmed = line.trim().to_string();
                        if !trimmed.is_empty() {
                            log_to_console(
                                &stderr_app,
                                &format!("[sidecar stderr] {}", trimmed),
                            );
                        }
                    }
                    Err(_) => break,
                }
            }
        });
    }
}

/// Windows：把子进程绑定到进程级唯一的 Job Object（KILL_ON_JOB_CLOSE）。
///
/// Job 句柄由 `OnceLock` 常驻、故意永不 close：仅当 Tauri 进程退出（含崩溃 / 被强杀）
/// 时由 OS 关闭最后一个句柄，触发 KILL_ON_JOB_CLOSE 连带终止所有已 assign 的 sidecar，
/// 从而根除「父死子留」的孤儿进程。任何一步失败都只 eprintln 告警、不阻断启动
/// （退化为无孤儿防护，功能不受影响）。
#[cfg(windows)]
fn assign_to_kill_on_close_job(child: &Child) {
    use std::os::windows::io::AsRawHandle;
    use std::sync::OnceLock;
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::HANDLE;
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    // 存 isize（HANDLE 裸值）以满足 OnceLock 的 Send + Sync；0 表示创建失败。
    static JOB: OnceLock<isize> = OnceLock::new();

    let job_raw = *JOB.get_or_init(|| unsafe {
        let job = match CreateJobObjectW(None, PCWSTR::null()) {
            Ok(h) => h,
            Err(e) => {
                eprintln!("[sidecar] CreateJobObject failed: {e}");
                return 0;
            }
        };
        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if let Err(e) = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        ) {
            eprintln!("[sidecar] SetInformationJobObject failed: {e}");
        }
        job.0 as isize
    });

    if job_raw == 0 {
        return; // Job 创建失败：退化为无 Job（不阻断启动）
    }

    unsafe {
        let hprocess = HANDLE(child.as_raw_handle() as _);
        if let Err(e) = AssignProcessToJobObject(HANDLE(job_raw as _), hprocess) {
            eprintln!("[sidecar] AssignProcessToJobObject failed: {e}");
        }
    }
}

/// PANDAPAL_READY 握手解析结果
pub struct ReadyInfo {
    pub token: String,
    pub model: String,
    pub provider: String,
}

/// 解析 "PANDAPAL_READY [token=<jwt>] [model=<id>] [provider=<name>]"
/// 各字段以空格分隔的 key=value，缺失字段回退为空字符串。
fn parse_ready_signal(line: &str) -> Option<ReadyInfo> {
    let trimmed = line.trim();
    if !trimmed.starts_with("PANDAPAL_READY") {
        return None;
    }
    let rest = trimmed.trim_start_matches("PANDAPAL_READY").trim();

    let mut info = ReadyInfo {
        token: String::new(),
        model: String::new(),
        provider: String::new(),
    };
    for part in rest.split_whitespace() {
        if let Some((key, value)) = part.split_once('=') {
            match key {
                "token" => info.token = value.to_string(),
                "model" => info.model = value.to_string(),
                "provider" => info.provider = value.to_string(),
                _ => {}
            }
        }
    }
    Some(info)
}

/// 向前端 console 发日志
fn log_to_console<R: Runtime>(app: &AppHandle<R>, msg: &str) {
    let _ = app.emit("sidecar-log", msg.to_string());
    eprintln!("{}", msg);
}

/// 优雅关闭 sidecar（onedir 单进程模型，大幅简化）
///
/// 流程：
/// 1. 置 kill_flag（告知 stdout 线程本次终止是预期的）
/// 2. Unix: SIGTERM / Windows: 关闭 stdin pipe（触发 Python EOF → shutdown）
/// 3. 非阻塞轮询等待退出（最多 2 秒）
/// 4. 超时则 SIGKILL / TerminateProcess 强杀
pub fn shutdown_sidecar<R: Runtime>(app: &AppHandle<R>) {
    let state = app.state::<Mutex<SidecarState>>();
    let mut s = state.lock().unwrap();

    // 标记：本次终止是主动 kill，stdout 线程不应 emit backend-crashed
    s.kill_flag.store(true, Ordering::Release);

    // Windows: 先关闭 stdin pipe 触发 Python EOF
    #[cfg(windows)]
    {
        drop(s.stdin_handle.take());
    }
    // Unix: 也 drop stdin（辅助触发 EOF），但主要依赖 SIGTERM
    #[cfg(unix)]
    {
        drop(s.stdin_handle.take());
    }

    if let Some(mut child) = s.child.take() {
        let pid = child.id();

        #[cfg(unix)]
        {
            // SIGTERM — 给 Python 优雅退出的机会
            unsafe { libc::kill(pid as i32, libc::SIGTERM); }
        }

        // 等待退出（非阻塞轮询，最多 2 秒）
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            match child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(50));
                }
                _ => {
                    // 超时或错误：强制终止
                    let _ = child.kill();
                    let _ = child.wait();
                    break;
                }
            }
        }

        eprintln!("[sidecar] shutdown complete (pid={})", pid);
    }

    // ★ 清理内存 token：旧进程已终止，残留的 BackendToken 会污染后续 BackendProvider
    // 的 warm-start 探测，导致 onBackendReady 基于无效 token 被提前调用。
    clear_backend_token(app);
}

/// 获取当前登录用户的 user_id（用于填充 session_id 等字段）
pub fn get_user_id<R: Runtime>(app: &AppHandle<R>) -> Result<String, String> {
    let state = app.state::<Mutex<SidecarState>>();
    let s = state.lock().map_err(|e| format!("lock failed: {}", e))?;
    if s.user_id.is_empty() {
        Err("user_id not set".to_string())
    } else {
        Ok(s.user_id.clone())
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// SESSION_ID 契约（见项目根 CLAUDE.md「SESSION_ID 契约」/ docs/session-id-契约.md）：
//   · session_id 的权威创建方是【后端】（SessionListManager.create_empty_session，
//     产出 sess-* 格式）。Rust 只负责记住「用户当前正在看哪个会话」这一视图状态。
//   · 会话级决策命令（HITL / 交互回复 / Plan 审批 / 停止生成）必须由前端显式携带
//     session_id，缺失即报错——**绝不**回退到当前视图或惰性 mint（否则跨会话污染）。
//   · get / create 彻底分离：`current_session_id`（纯读）与 `create_session_id`（纯创建）
//     各司其职，**没有** get-or-create 混合体（名为 get 实则偷偷 mint 是最隐蔽的隐患）。
//     只有 send_message（发起方）在冷启动无会话时**显式**调 create 兜底，应逐步由
//     「后端下发初始 sess- 会话」取代。
// ─────────────────────────────────────────────────────────────────────────────

/// 纯读：返回「当前正在看的会话」，可能为空字符串（**绝不 mint**）。
///
/// 用于 session_id 仅作附带元数据、后端并不据此路由的命令（技能 CRUD / 搜索 /
/// 定时任务查询等）。这些命令即便拿到空串也无副作用。
pub fn current_session_id<R: Runtime>(app: &AppHandle<R>) -> Result<String, String> {
    let state = app.state::<Mutex<SidecarState>>();
    let s = state.lock().map_err(|e| format!("lock failed: {}", e))?;
    Ok(s.session_id.clone())
}

/// 纯创建：mint 一个新的会话 id（canonical 格式 sess-{uuid}），存为当前视图并返回。
///
/// ★ 与「读」彻底分离：本函数**永远创建新 id**，绝不读旧值、绝不「有就用没有才建」
/// （那种 get-or-create 混合体名实不符，是最隐蔽的隐患）。仅供 send_message 冷启动兜底
/// （无当前会话时）显式调用；产出与后端 SessionListManager 同为 sess- 前缀，全系统单一格式。
pub fn create_session_id<R: Runtime>(app: &AppHandle<R>) -> Result<String, String> {
    let state = app.state::<Mutex<SidecarState>>();
    let mut s = state.lock().map_err(|e| format!("lock failed: {}", e))?;
    if s.user_id.is_empty() {
        return Err("user_id not set".to_string());
    }
    let sid = format!("sess-{}", uuid::Uuid::new_v4().simple());
    s.session_id = sid.clone();
    Ok(sid)
}

/// 覆盖 session_id（前端主动切换 UI 会话时调用）。
///
/// v003 会话列表引入：Rust 层不再"独占"session 生成，允许前端把当前视图
/// session_id 同步过来，使得 send_message 使用用户实际选择的会话。
pub fn override_session_id<R: Runtime>(app: &AppHandle<R>, sid: String) -> Result<(), String> {
    let state = app.state::<Mutex<SidecarState>>();
    let mut s = state.lock().map_err(|e| format!("lock failed: {}", e))?;
    s.session_id = sid;
    Ok(())
}

/// 设置内存中的 BackendToken（token 刷新时调用，与 store 保持同步）。
pub fn set_backend_token<R: Runtime>(app: &AppHandle<R>, token: &str) {
    let token_state = app.state::<BackendToken>();
    *token_state.0.lock().unwrap() = token.to_string();
    eprintln!("[sidecar] backend token updated");
}

/// 清除内存中的 BackendToken（登出时调用）
pub fn clear_backend_token<R: Runtime>(app: &AppHandle<R>) {
    let token_state = app.state::<BackendToken>();
    *token_state.0.lock().unwrap() = String::new();
    eprintln!("[sidecar] backend token cleared");
}
