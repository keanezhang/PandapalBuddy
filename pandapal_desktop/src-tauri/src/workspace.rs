//! pandapal-desktop/src-tauri/src/workspace.rs
//!
//! 工作区（用户「打开文件夹」选定的目录）管理。
//!
//! 设计（一进程一目录 / Method A）：
//! - Agent 文件工具的根目录只能由用户显式选择，不做任何探测。
//! - `open_workspace` 只负责**校验目录 + 记录路径 + 持久化**，不启动 sidecar。
//!   sidecar 的启动由 `start_sidecar` 命令在 LLM 凭据检查通过后触发，
//!   确保 sidecar 启动时 toml 一定已配置好，不会因无凭据崩溃。
//! - 切换工作区 = kill 当前 sidecar 后用新 --workdir 重启（由 start_sidecar 执行）。
//! - 打开相同工作区 = 仅更新记录，不杀进程。
//!
//! 持久化：workspace_store.json
//!   - last_workspace:     Option<String>  上次打开的工作区（用于自动恢复）
//!   - recent_workspaces:  Vec<String>     最近打开列表（最新在前，去重，上限 10）

use std::path::Path;

use serde::Serialize;
use tauri::{AppHandle, Manager};
use tauri_plugin_store::StoreExt;

use crate::auth;
use crate::lifecycle;
use crate::sidecar;

const WORKSPACE_STORE_FILE: &str = "workspace_store.json";
const LAST_KEY: &str = "last_workspace";
const RECENT_KEY: &str = "recent_workspaces";
const RECENT_MAX: usize = 10;

/// 返回给前端的「最近工作区」快照
#[derive(Serialize, Clone, Default)]
pub struct RecentWorkspaces {
    /// 上次打开的工作区（若仍存在）
    pub last: Option<String>,
    /// 最近打开列表（最新在前，已过滤掉不存在的目录）
    pub recent: Vec<String>,
}

/// 打开（或切换到）一个工作区。
///
/// **不启动 sidecar** —— 只校验目录、记录路径到 SidecarState、持久化。
/// sidecar 启动由 `start_sidecar` 命令在 LLM 凭据检查通过后触发。
/// 返回规范化后的工作区绝对路径。
#[tauri::command]
pub fn open_workspace(app: AppHandle, path: String) -> Result<String, String> {
    // 1. 校验：必须是已存在的目录
    let p = Path::new(&path);
    if !p.is_dir() {
        return Err(format!("路径不存在或不是文件夹：{}", path));
    }
    let workdir = p.to_string_lossy().to_string();

    // 2. 必须已登录（user_id 用于后续 sidecar 启动 + toml 路径定位）
    let (user_id, _token) = auth::read_credentials(&app)
        .ok_or_else(|| "尚未登录，无法打开工作区".to_string())?;

    // 3. 记录工作区到 SidecarState；若之前有 sidecar 在跑（切换工作区），标记需要 kill
    let need_kill = {
        let state = app.state::<std::sync::Mutex<sidecar::SidecarState>>();
        let mut s = state.lock().map_err(|e| format!("Lock error: {}", e))?;
        let was_running = s.child.is_some();
        s.workspace = workdir.clone();
        s.user_id = user_id.clone();
        was_running
    };

    // 4. 切换工作区：先杀掉旧 sidecar（一进程一目录）。新 sidecar 由 start_sidecar 启动。
    if need_kill {
        lifecycle::kill_sidecar_only(&app);
        lifecycle::reset_shutdown_flag();
    }

    // 5. 持久化（last + recent）
    persist(&app, &workdir)?;

    Ok(workdir)
}

/// 读取最近工作区（供前端在登录后决定「自动恢复 / 展示打开文件夹」）。
#[tauri::command]
pub fn get_recent_workspaces(app: AppHandle) -> Result<RecentWorkspaces, String> {
    let store = app
        .store(WORKSPACE_STORE_FILE)
        .map_err(|e| format!("打开 store 失败: {}", e))?;

    let last = store
        .get(LAST_KEY)
        .and_then(|v| v.as_str().map(String::from))
        .filter(|s| Path::new(s).is_dir());

    let recent: Vec<String> = store
        .get(RECENT_KEY)
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .filter_map(|v| v.as_str().map(String::from))
        .filter(|s| Path::new(s).is_dir())
        .collect();

    Ok(RecentWorkspaces { last, recent })
}

/// 返回当前**存活进程**正在服务的工作区（空字符串表示尚未打开 / 进程已退出）。
///
/// 必须以 child 是否存活为准：shutdown 只 take() child 而不清 workspace 字段，
/// 若仅看 workspace 会在登出后返回残留路径，导致门控被错误跳过。
#[tauri::command]
pub fn get_current_workspace(app: AppHandle) -> Result<String, String> {
    let state = app.state::<std::sync::Mutex<sidecar::SidecarState>>();
    let s = state.lock().map_err(|e| format!("Lock error: {}", e))?;
    if s.child.is_some() {
        Ok(s.workspace.clone())
    } else {
        Ok(String::new())
    }
}

/// 写入 last_workspace，并把该路径提到 recent 列表最前（去重、限长）。
fn persist(app: &AppHandle, workdir: &str) -> Result<(), String> {
    let store = app
        .store(WORKSPACE_STORE_FILE)
        .map_err(|e| format!("打开 store 失败: {}", e))?;

    let mut recent: Vec<String> = store
        .get(RECENT_KEY)
        .and_then(|v| v.as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .filter_map(|v| v.as_str().map(String::from))
        .collect();

    recent.retain(|s| s != workdir);
    recent.insert(0, workdir.to_string());
    recent.truncate(RECENT_MAX);

    store.set(LAST_KEY, serde_json::json!(workdir));
    store.set(RECENT_KEY, serde_json::json!(recent));
    store
        .save()
        .map_err(|e| format!("保存 store 失败: {}", e))?;

    Ok(())
}
