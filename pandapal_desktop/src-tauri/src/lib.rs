//! pandapal-desktop/src-tauri/src/lib.rs
//!
//! Tauri app 入口逻辑（desktop + mobile 共用）。
//!
//! 职责：
//! - 注册 State（SidecarState、BackendToken）
//! - setup 钩子：初始化托盘
//! - 窗口关闭行为：隐藏到托盘（而非退出进程）
//! - 退出统一由 lifecycle::request_shutdown 管理
//! - 提供 Tauri commands：
//!   - get_auth_token（后端就绪查询）
//!   - send_message / send_hitl_decision / stop_generation（IPC 通信）
//!   - quit_app（退出）

use std::sync::Mutex;
use tauri::{
    AppHandle, Manager, RunEvent, WebviewWindow,
};
mod auth;
mod lifecycle;
mod pets;
mod sidecar;
mod tray;
mod workspace;

use sidecar::{BackendModel, BackendToken, SidecarState};

/// 从登录态获取 user_id（失败则直接返回错误，不做 fallback）
fn require_user_id(app: &AppHandle) -> Result<String, String> {
    sidecar::get_user_id(app)
}

// ── 查询类 Commands ──────────────────────────────────────────────────────────

/// Tauri command：前端查询鉴权 Token（后端就绪后可调用）
#[tauri::command]
fn get_auth_token(state: tauri::State<BackendToken>) -> String {
    state.0.lock().unwrap().clone()
}

// ── 工作区 + LLM 凭据 Commands（配置就绪前，不依赖 sidecar）─────────────────────

/// 启动 sidecar（在 LLM 凭据检查通过后由前端 CredentialGate 触发）。
///
/// 从 SidecarState 读取已记录的 workspace，从 auth store 读取 user_id + token，
/// 调用 sidecar::spawn_sidecar 启动 Python 进程。
/// 幂等：若 sidecar 已在运行，spawn_sidecar 内部会重新 emit backend-ready。
#[tauri::command]
fn start_sidecar(app: AppHandle) -> Result<(), String> {
    let workspace = {
        let state = app.state::<Mutex<SidecarState>>();
        let s = state.lock().map_err(|e| format!("Lock error: {}", e))?;
        if s.workspace.is_empty() {
            return Err("workspace not set, call open_workspace first".to_string());
        }
        s.workspace.clone()
    };
    let (user_id, token) = auth::read_credentials(&app)
        .ok_or_else(|| "尚未登录，无法启动 sidecar".to_string())?;
    let app_data_dir = app.path().app_data_dir()
        .map_err(|e| format!("无法获取应用数据目录: {}", e))?
        .to_string_lossy()
        .to_string();
    sidecar::spawn_sidecar(&app, &user_id, &token, &workspace, &app_data_dir);
    Ok(())
}

/// 从 AppData + SidecarState 取 user_id，拼出凭据 toml 路径。
fn cred_toml_path(app: &AppHandle) -> Result<std::path::PathBuf, String> {
    let user_id = {
        let state = app.state::<Mutex<SidecarState>>();
        let s = state.lock().map_err(|e| format!("Lock error: {}", e))?;
        if s.user_id.is_empty() {
            return Err("user_id not set".to_string());
        }
        s.user_id.clone()
    };
    let app_data_dir = app.path().app_data_dir()
        .map_err(|e| format!("无法获取应用数据目录: {}", e))?;
    Ok(app_data_dir
        .join("users")
        .join(&user_id)
        .join("credentials")
        .join("llm_credentials.toml"))
}

/// toml 解析用结构体（仅用于 check_llm_credentials 的"是否已配置"判断）。
#[derive(serde::Deserialize)]
struct CredFile {
    #[serde(default)]
    credentials: Vec<CredEntry>,
}
#[derive(serde::Deserialize)]
struct CredEntry {
    #[serde(default)]
    provider: String,
    #[serde(default)]
    api_key: String,
    #[serde(default)]
    model_id: String,
}

/// 检查 LLM 凭据是否已配置（不依赖 sidecar，Rust 直接读 toml 文件）。
///
/// 返回 `{ configured: bool }`。文件不存在 / 解析失败 / 无合法条目 → false。
#[tauri::command]
fn check_llm_credentials(app: AppHandle) -> Result<serde_json::Value, String> {
    let path = cred_toml_path(&app)?;
    let content = match std::fs::read_to_string(&path) {
        Ok(c) => c,
        Err(_) => return Ok(serde_json::json!({ "configured": false })),
    };
    let file: CredFile = toml::from_str(&content).unwrap_or(CredFile { credentials: vec![] });
    let configured = file.credentials.iter().any(|c| {
        !c.provider.is_empty() && !c.api_key.is_empty() && !c.model_id.is_empty()
    });
    Ok(serde_json::json!({ "configured": configured }))
}

/// 前端传入的凭据条目（save_llm_credentials 用）。
///
/// `api_key` 为 `Option`：**省略表示「保持原值不变」**（脱敏 sentinel 机制）。
/// 前端对未编辑的密钥不提交该字段，由 Rust 按 (provider, model_id) 取回旧值——
/// 用户的真实 key 永不经过前端，也就不可能被脱敏值覆盖。
#[derive(serde::Deserialize)]
struct CredInput {
    provider: String,
    #[serde(default)]
    api_key: Option<String>,
    model_id: String,
    #[serde(default)]
    base_url: Option<String>,
    is_default: bool,
    // 单价（CNY / 1k token）。留空则回落系统默认表；系统表也没有则拒绝保存。
    #[serde(default)]
    input_price_per_1k: Option<f64>,
    #[serde(default)]
    output_price_per_1k: Option<f64>,
    #[serde(default)]
    cache_read_price_per_1k: Option<f64>,
}

/// 脱敏标记：`_mask_key` 产出的中缀。提交体中出现即判定为「脱敏值回写」。
const MASK_MARKER: &str = "***";

// ── Provider 白名单（单一真相源 = provider_catalog.toml）──────────────────────
//
// 设计约束（PRD §模型管理 规则 1）：
//   - provider 固定：白名单 = provider_catalog.toml 定义的 provider id 集合
//   - 不再硬编码（旧 ALLOWED_PROVIDERS 常量已删除，新增/移除 provider 只改 toml 一处）
//   - Python 与 Rust 共读同一份 toml，杜绝前后端白名单散落重复
//
// 实现：
//   - **运行时**读取 toml（打包为 Tauri resource），OnceLock 缓存解析结果
//   - ⚠️ 刻意**不用** include_str!：编译期嵌入会把 toml 变成一份拷贝，
//     「旧二进制 + 新配置」时 Rust 与 Python 的白名单会不一致且无运行时检测手段。
//     单一真相源指的是「这一个文件」，不是「某个进程」——两方读同一路径才成立。
//   - 打包路径见 tauri.conf.json bundle.resources（config/*.toml）；
//     开发态回落到工作区源码路径。

/// toml 解析用：provider 完整元信息（serde 忽略 toml 里多余的 env_prefix / verify_url 字段）。
#[derive(serde::Deserialize, serde::Serialize)]
struct CatalogProvider {
    id: String,
    display_name: String,
    guide_url: String,
    default_base_url: String,
}

#[derive(serde::Deserialize)]
struct CatalogFile {
    #[serde(default)]
    providers: Vec<CatalogProvider>,
}

/// 系统默认单价条目（model_prices.toml 的 [[prices]]）。
///
/// ⚠️ **本表不是白名单**：表中没有的 model_id 照样可配置、可保存、可使用，
/// 只是保存时必须由用户填写单价。它的两个用途都是**引导性质**：
///   ① 单价三级回落的第 ② 级；② model_id combobox 的推荐清单。
#[derive(serde::Deserialize, serde::Serialize, Clone)]
struct ModelPriceEntry {
    model_id: String,
    provider: String,
    input_price_per_1k: f64,
    output_price_per_1k: f64,
    #[serde(default)]
    cache_read_price_per_1k: Option<f64>,
}

#[derive(serde::Deserialize)]
struct ModelPricesFile {
    exchange_rate_usd: f64,
    #[serde(default)]
    prices: Vec<ModelPriceEntry>,
}

/// 首次解析结果缓存（OnceLock 保证只解析一次）。
static PROVIDER_CATALOG: std::sync::OnceLock<Vec<CatalogProvider>> = std::sync::OnceLock::new();
static MODEL_PRICES: std::sync::OnceLock<Option<ModelPricesFile>> = std::sync::OnceLock::new();

/// 定位随包发布的系统配置 toml。
///
/// 打包态：Tauri resource 目录 `config/{name}`；
/// 开发态：回落到工作区源码路径 `<crate>/../../pandapal/config/llm/{name}`，
/// 与 Python 端 `Path(__file__).parent / name` 指向**同一个文件**。
fn system_config_path(app: &AppHandle, name: &str) -> Option<std::path::PathBuf> {
    if let Ok(p) = app.path().resolve(
        format!("config/{}", name),
        tauri::path::BaseDirectory::Resource,
    ) {
        if p.exists() {
            return Some(p);
        }
    }
    let dev = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../pandapal/config/llm")
        .join(name);
    if dev.exists() {
        return Some(dev);
    }
    None
}

fn read_system_config(app: &AppHandle, name: &str) -> Option<String> {
    let path = system_config_path(app, name)?;
    match std::fs::read_to_string(&path) {
        Ok(s) => Some(s),
        Err(e) => {
            eprintln!("[config] 读取 {:?} 失败: {}", path, e);
            None
        }
    }
}

/// 返回系统预置 provider 完整元信息列表（从 provider_catalog.toml 派生）。
///
/// 替代旧硬编码 `ALLOWED_PROVIDERS`。新增/移除 provider 只改 toml 一处。
///
/// ⚠️ 解析失败 / providers 为空时返回空表，此时 `is_allowed_provider` 对一切
/// provider 返回 false（**fail-closed**，绝不放行未知 provider）。调用方须把
/// 空表当作「系统配置损坏」明确报错——历史实现只 eprintln 一行就静默继续，
/// 结果是用户看到一个全空的下拉框、配不上任何模型却得不到任何解释。
fn provider_catalog(app: &AppHandle) -> &'static [CatalogProvider] {
    PROVIDER_CATALOG.get_or_init(|| {
        let Some(content) = read_system_config(app, "provider_catalog.toml") else {
            eprintln!("[config] provider_catalog.toml 缺失，provider 白名单为空");
            return vec![];
        };
        match toml::from_str::<CatalogFile>(&content) {
            Ok(f) if !f.providers.is_empty() => f.providers,
            Ok(_) => {
                eprintln!("[config] provider_catalog.toml 的 providers 为空");
                vec![]
            }
            Err(e) => {
                eprintln!("[config] provider_catalog.toml 解析失败: {}", e);
                vec![]
            }
        }
    })
}

/// 系统默认单价表（可为 None —— 那只意味着「所有模型都要用户自己填价」）。
fn model_prices(app: &AppHandle) -> Option<&'static ModelPricesFile> {
    MODEL_PRICES
        .get_or_init(|| {
            let content = read_system_config(app, "model_prices.toml")?;
            match toml::from_str::<ModelPricesFile>(&content) {
                Ok(f) if f.exchange_rate_usd > 0.0 => Some(f),
                Ok(f) => {
                    // 汇率属金额类字段：非法即报错，绝不默认回落 7.0。
                    eprintln!(
                        "[config] model_prices.toml 汇率非法: {}",
                        f.exchange_rate_usd
                    );
                    None
                }
                Err(e) => {
                    eprintln!("[config] model_prices.toml 解析失败: {}", e);
                    None
                }
            }
        })
        .as_ref()
}

/// 该 model_id 是否有系统默认单价（决定「用户要不要自己填价」，**不决定能否使用**）。
fn has_system_price(app: &AppHandle, model_id: &str) -> bool {
    model_prices(app)
        .map(|f| f.prices.iter().any(|p| p.model_id == model_id))
        .unwrap_or(false)
}

/// 判断 provider 是否在系统预置白名单内（fail-closed）。
fn is_allowed_provider(app: &AppHandle, provider: &str) -> bool {
    provider_catalog(app).iter().any(|p| p.id == provider)
}

/// 前端拉取系统预置 provider 元信息（凭据表单 provider 下拉源）。
///
/// 走 Rust 命令**运行时**读 toml，不依赖 sidecar，
/// 首次配置场景（sidecar 未启动）也能拉到。
///
/// 返回 `{ providers: [{ id, display_name, guide_url, default_base_url }] }`，
/// 不含 env_prefix / verify_url（后端专用字段不下发）。
/// 配置损坏时返回 Err，前端据此显示「系统配置损坏」而非无限加载。
#[tauri::command]
fn get_provider_catalog(app: AppHandle) -> Result<serde_json::Value, String> {
    let providers = provider_catalog(&app);
    if providers.is_empty() {
        return Err("系统配置损坏：provider_catalog.toml 缺失或为空".to_string());
    }
    Ok(serde_json::json!({ "providers": providers }))
}

/// 前端拉取系统默认单价表（model_id combobox 推荐清单 + 默认价展示）。
///
/// ⚠️ 前端**不得**用本表限制用户可填的 model_id——表外模型必须仍可填写与保存。
#[tauri::command]
fn get_model_prices(app: AppHandle) -> serde_json::Value {
    match model_prices(&app) {
        Some(f) => serde_json::json!({
            "exchange_rate_usd": f.exchange_rate_usd,
            "prices": f.prices,
        }),
        // 表不可用不是致命错误：退化为「所有模型都要用户填价」。
        None => serde_json::json!({ "exchange_rate_usd": null, "prices": [] }),
    }
}

/// 保存 LLM 凭据到 toml 文件（不依赖 sidecar，Rust 直接写）。
///
/// 校验 → 生成 TOML → 原子写入。用于首次配置（sidecar 尚未启动时）。
#[tauri::command]
fn save_llm_credentials(
    app: AppHandle,
    credentials: Vec<CredInput>,
) -> Result<(), String> {
    // 1. 校验
    if credentials.is_empty() {
        return Err("至少需要一组凭据".to_string());
    }
    // 旧 key 表：供 sentinel 机制取回「未修改」的密钥（按 (provider, model_id) 定位）。
    let existing_keys = load_existing_keys(&app);

    // 主键 (provider, model_id)：同一 provider 可配多个模型（旧实现按 provider 去重，
    // 导致全系统最多 4 个模型，「有什么用什么」无法成立）。
    let mut seen_keys = std::collections::HashSet::new();
    // 路由键 model_id 必须全局唯一：它是 LLMRouter 的键，跨 provider 重名会导致
    // 「装配了 A 却路由到 B」的静默错配（费用记到错误的 provider 账上）。
    let mut seen_model_ids = std::collections::HashSet::new();
    let mut default_count = 0;
    let mut resolved_keys: Vec<String> = Vec::with_capacity(credentials.len());

    for (i, c) in credentials.iter().enumerate() {
        if !is_allowed_provider(&app, &c.provider) {
            let allowed: Vec<&str> =
                provider_catalog(&app).iter().map(|p| p.id.as_str()).collect();
            if allowed.is_empty() {
                return Err(
                    "系统配置损坏：provider_catalog.toml 缺失或为空，无法校验 provider"
                        .to_string(),
                );
            }
            return Err(format!(
                "credentials[{}]: provider={} 不在白名单 {:?}",
                i, c.provider, allowed
            ));
        }
        let model_id = c.model_id.trim();
        if model_id.is_empty() {
            return Err(format!(
                "credentials[{}]({}): model_id 缺失",
                i, c.provider
            ));
        }
        if !seen_keys.insert((c.provider.as_str(), model_id)) {
            return Err(format!(
                "credentials[{}]: (provider={}, model_id={}) 重复",
                i, c.provider, model_id
            ));
        }
        if !seen_model_ids.insert(model_id) {
            return Err(format!(
                "credentials[{}]: model_id={} 已被其他 provider 使用；\
                 model_id 同时是路由键，必须全局唯一",
                i, model_id
            ));
        }

        // ── api_key：sentinel 合并 + 脱敏值拦截 ──
        let api_key = match c.api_key.as_deref().map(str::trim) {
            Some(k) if !k.is_empty() => {
                // 第二道防线：即便前端 sentinel 失效（或提交体被篡改），也绝不让
                // 脱敏值覆盖真实 key。历史事故：用户只改 model_id，全部真 key 被
                // 覆写为 sk-a***bcd 且不可恢复——它能通过旧版仅有的「长度 ≥8」校验。
                if k.contains(MASK_MARKER) {
                    return Err(format!(
                        "credentials[{}]({}/{}): api_key 疑似脱敏值，拒绝写入。\
                         未修改密钥时请省略该字段",
                        i, c.provider, model_id
                    ));
                }
                if k.len() < 8 {
                    return Err(format!(
                        "credentials[{}]({}/{}): api_key 长度不足",
                        i, c.provider, model_id
                    ));
                }
                k.to_string()
            }
            // 省略 api_key → 沿用旧值；找不到旧值说明是新增凭据，必须提供
            _ => existing_keys
                .get(&(c.provider.clone(), model_id.to_string()))
                .cloned()
                .ok_or_else(|| {
                    format!(
                        "credentials[{}]({}/{}): 省略了 api_key 但现有配置中\
                         找不到该模型，新增凭据必须提供 api_key",
                        i, c.provider, model_id
                    )
                })?,
        };
        resolved_keys.push(api_key);

        // ── 单价三级回落：① 用户填 ② 系统默认表 ③ 拒绝保存 ──
        match (c.input_price_per_1k, c.output_price_per_1k) {
            (Some(inp), Some(out)) => {
                if inp < 0.0 || out < 0.0 || c.cache_read_price_per_1k.unwrap_or(0.0) < 0.0 {
                    return Err(format!(
                        "credentials[{}]({}/{}): 单价必须 ≥ 0",
                        i, c.provider, model_id
                    ));
                }
            }
            (None, None) => {
                // 「只填缓存价」拦截：缓存价单独存在无法计费，Python 侧
                // resolve_effective_price 会直接落到系统默认表，用户填的数被
                // 静默丢弃（界面回显用户值、实际按系统价计费），且绕过负值校验。
                // 与 Python 同步拦截，避免半套价写进 toml（§九 金额类零默认）。
                if c.cache_read_price_per_1k.is_some() {
                    return Err(format!(
                        "credentials[{}]({}/{}): 只填了缓存命中价；\
                         自定义单价必须同时提供输入价与输出价",
                        i, c.provider, model_id
                    ));
                }
                // ⚠️ 这里查的是「有没有默认价」，**不是**「模型在不在白名单里」。
                // 表外模型只要用户填了价就能保存——绝不能退化成可用性白名单。
                if !has_system_price(&app, model_id) {
                    return Err(format!(
                        "credentials[{}]({}/{}): 该模型无系统默认单价，\
                         请填写输入价与输出价（单位 CNY/1k token）",
                        i, c.provider, model_id
                    ));
                }
            }
            // 半套价：只填一个等于「输入免费」或「输出免费」，几乎必然是误填。
            _ => {
                return Err(format!(
                    "credentials[{}]({}/{}): 输入价与输出价必须同时填写",
                    i, c.provider, model_id
                ))
            }
        }

        if let Some(ref url) = c.base_url {
            let url = url.trim();
            if !url.is_empty() && !url.starts_with("http://") && !url.starts_with("https://") {
                return Err(format!(
                    "credentials[{}]({}): base_url 必须以 http:// 或 https:// 开头",
                    i, c.provider
                ));
            }
        }
        if c.is_default {
            default_count += 1;
        }
    }
    if default_count != 1 {
        return Err(format!(
            "必须有且仅有一组凭据设为默认，当前有 {} 组",
            default_count
        ));
    }

    // 2. 生成 TOML 内容（resolved_keys 与 credentials 逐项对应，含 sentinel 取回的旧 key）
    let toml_content = build_toml_content(&credentials, &resolved_keys);

    // 3. 原子写入
    let toml_path = cred_toml_path(&app)?;
    let dir = toml_path.parent().ok_or("无效的凭据路径")?;
    std::fs::create_dir_all(dir).map_err(|e| format!("创建目录失败: {}", e))?;

    let tmp_path = dir.join(format!(".llm_credentials_tmp_{}", uuid::Uuid::new_v4().simple()));
    std::fs::write(&tmp_path, &toml_content).map_err(|e| {
        let _ = std::fs::remove_file(&tmp_path);
        format!("写入临时文件失败: {}", e)
    })?;
    std::fs::rename(&tmp_path, &toml_path).map_err(|e| {
        let _ = std::fs::remove_file(&tmp_path);
        format!("重命名凭据文件失败: {}", e)
    })?;

    eprintln!("[cred] saved {} credentials to {:?}", credentials.len(), toml_path);
    Ok(())
}

/// 读取现有凭据的真实 api_key，供 sentinel 机制取回「未修改」的密钥。
///
/// 返回 (provider, model_id) → api_key。文件不存在 / 解析失败返回空表——
/// 此时省略 api_key 的条目会被判为「新增凭据缺 key」而报错，不会写入空 key。
fn load_existing_keys(app: &AppHandle) -> std::collections::HashMap<(String, String), String> {
    let mut map = std::collections::HashMap::new();
    let Ok(path) = cred_toml_path(app) else {
        return map;
    };
    let Ok(content) = std::fs::read_to_string(&path) else {
        return map;
    };
    let Ok(file) = toml::from_str::<CredFile>(&content) else {
        return map;
    };
    for c in file.credentials {
        if !c.provider.is_empty() && !c.model_id.is_empty() && !c.api_key.is_empty() {
            map.insert((c.provider, c.model_id), c.api_key);
        }
    }
    map
}

/// 生成 TOML 文件内容（与 Python 端 CredentialStore._build_toml_content 格式一致）。
///
/// `resolved_keys` 与 `credentials` **逐项对应**，是 sentinel 合并后的真实 key。
fn build_toml_content(credentials: &[CredInput], resolved_keys: &[String]) -> String {
    let mut lines: Vec<String> = vec![
        "# LLM credentials for PandaPal (user-managed)".to_string(),
        "# This file is auto-generated. Do not edit manually.".to_string(),
        String::new(),
    ];

    // default_model_id（而非 default_provider）：切换的粒度是模型，不是 provider。
    let default_model_id = credentials
        .iter()
        .find(|c| c.is_default)
        .map(|c| c.model_id.trim())
        .unwrap_or("");
    if !default_model_id.is_empty() {
        lines.push(format!("default_model_id = {}", toml_str(default_model_id)));
        lines.push(String::new());
    }

    for (i, c) in credentials.iter().enumerate() {
        let api_key = resolved_keys.get(i).map(String::as_str).unwrap_or("");
        let model_id = c.model_id.trim();
        if c.provider.is_empty() || api_key.is_empty() || model_id.is_empty() {
            continue;
        }
        lines.push("[[credentials]]".to_string());
        lines.push(format!("provider = {}", toml_str(&c.provider)));
        lines.push(format!("api_key = {}", toml_str(api_key)));
        lines.push(format!("model_id = {}", toml_str(model_id)));
        if let Some(ref url) = c.base_url {
            let url = url.trim();
            if !url.is_empty() {
                lines.push(format!("base_url = {}", toml_str(url)));
            }
        }
        for (name, value) in [
            ("input_price_per_1k", c.input_price_per_1k),
            ("output_price_per_1k", c.output_price_per_1k),
            ("cache_read_price_per_1k", c.cache_read_price_per_1k),
        ] {
            if let Some(v) = value {
                lines.push(format!("{} = {}", name, v));
            }
        }
        lines.push(String::new());
    }

    lines.join("\n") + "\n"
}

/// TOML 双引号字符串转义
fn toml_str(s: &str) -> String {
    let escaped = s.replace('\\', "\\\\").replace('"', "\\\"");
    format!("\"{}\"", escaped)
}

// ── IPC 通信 Commands ─────────────────────────────────────────────────────────

/// 发送用户消息（前端 → Rust → Python stdin）
/// session_id：优先用前端携带值；无则纯读当前视图；冷启动无会话时显式创建 sess-{uuid}。
#[tauri::command]
fn send_message(
    app: AppHandle,
    msg_id: String,
    content: String,
    deep_thinking: Option<bool>,
    model_id: Option<String>,
    active_app_id: Option<String>,
    session_id: Option<String>,
    mode: Option<String>,
) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    // ★ get / create 彻底分离，不用 get-or-create 混合体：
    //   1) 前端携带了 session_id（正常路径，后端 bootstrap 保证）→ 记住并使用；
    //   2) 没带 → 纯读当前视图；
    //   3) 仍为空（冷启动、尚无任何会话）→ **显式创建**一个新会话（clearly a CREATE）。
    let sid = match session_id.filter(|s| !s.trim().is_empty()) {
        Some(sid_override) => {
            let sid_override = sid_override.trim().to_string();
            sidecar::override_session_id(&app, sid_override.clone())?;
            sid_override
        }
        None => {
            let current = sidecar::current_session_id(&app)?;
            if current.is_empty() {
                sidecar::create_session_id(&app)?
            } else {
                current
            }
        }
    };
    let mut payload = serde_json::json!({
        "type": "SEND_MESSAGE",
        "msg_id": msg_id,
        "content": content,
        "session_id": sid,
        "user_id": uid,
    });
    if let Some(ref aid) = active_app_id {
        if !aid.trim().is_empty() {
            payload["active_app_id"] = serde_json::json!(aid.trim());
        }
    }
    if let Some(ref mid) = model_id {
        if !mid.trim().is_empty() {
            payload["model_id"] = serde_json::json!(mid);
        }
    }
    if let Some(dt) = deep_thinking {
        payload["deep_thinking"] = serde_json::json!(dt);
    }
    if let Some(ref m) = mode {
        if !m.trim().is_empty() {
            payload["mode"] = serde_json::json!(m.trim());
        }
    }
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// v003：前端主动切换当前视图 session_id（后续 send_message 会用此 id）。
#[tauri::command]
fn set_current_session_id(
    app: AppHandle,
    session_id: String,
) -> Result<(), String> {
    sidecar::override_session_id(&app, session_id)
}

/// v003：通用会话列表 IPC 发送（前端 → Rust → Python stdin）。
///
/// 前端传入完整 payload（含 type/msg_id + 各自字段），Rust 只补 user_id 后转发。
#[tauri::command]
fn send_session_ipc(
    app: AppHandle,
    payload: serde_json::Value,
) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let mut p = payload;
    if let serde_json::Value::Object(ref mut obj) = p {
        obj.entry("user_id".to_string())
            .or_insert(serde_json::Value::String(uid));
    }
    sidecar::write_to_sidecar(&app, &p.to_string())
}

/// 发送 HITL 审批决策
#[tauri::command]
fn send_hitl_decision(
    app: AppHandle,
    msg_id: String,
    run_id: String,
    decision: String,
    approval_id: String,
    session_id: String,
) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let payload = serde_json::json!({
        "type": "HITL_DECISION",
        "msg_id": msg_id,
        "run_id": run_id,
        "decision": decision,
        "approval_id": approval_id,
        "session_id": session_id,
        "user_id": uid,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 发送交互型工具的用户回复
#[tauri::command]
fn send_interaction_response(
    app: AppHandle,
    msg_id: String,
    run_id: String,
    response: String,
    session_id: Option<String>,
) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    // ★ 问卷回复必须回到「提问的那个 session」，绝不能回退到当前正在看的会话：
    //   用户切走再作答会串到别的会话（与 plan 审批同源的跨会话污染）。
    let sid = session_id
        .filter(|s| !s.is_empty())
        .ok_or("session_id is required for interaction response")?;
    let payload = serde_json::json!({
        "type": "INTERACTION_RESPONSE",
        "msg_id": msg_id,
        "run_id": run_id,
        "response": response,
        "session_id": sid,
        "user_id": uid,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 发送 Plan Mode 审批决策（批准 / 完善 / 放弃）
#[tauri::command]
fn send_plan_approval_decision(
    app: AppHandle,
    msg_id: String,
    run_id: String,
    plan_action: String,
    session_id: Option<String>,
    user_id: Option<String>,
    user_text: Option<String>,
    edited_plan_content: Option<String>,
) -> Result<(), String> {
    let uid = user_id
        .filter(|s| !s.is_empty())
        .or_else(|| require_user_id(&app).ok())
        .ok_or("user_id is required")?;
    // ★ 审批决策必须携带「计划所属的 session_id」，绝不能回退到当前正在看的 session：
    //   用户提交长任务计划后往往会切走看别的会话，此时回退会把批准恢复路由到错误的
    //   session，造成跨会话串台。宁可显式报错（前端会打日志），也不静默误路由。
    let sid = session_id
        .filter(|s| !s.is_empty())
        .ok_or("session_id is required for plan approval decision")?;
    let payload = serde_json::json!({
        "type": "PLAN_APPROVAL_DECISION",
        "msg_id": msg_id,
        "run_id": run_id,
        "plan_action": plan_action,
        "session_id": sid,
        "user_id": uid,
        "user_text": user_text.unwrap_or_default(),
        "edited_plan_content": edited_plan_content,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 心跳 PING
#[tauri::command]
fn send_ping(app: AppHandle, msg_id: String) -> Result<(), String> {
    let payload = serde_json::json!({
        "type": "PING",
        "msg_id": msg_id,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// D1 Pull：前端请求定时任务列表
#[tauri::command]
#[allow(dead_code)]
fn request_scheduled_tasks(app: AppHandle, msg_id: String) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let sid = sidecar::current_session_id(&app)?;
    let payload = serde_json::json!({
        "type": "REQUEST_SCHEDULED_TASKS",
        "msg_id": msg_id,
        "user_id": uid,
        "session_id": sid,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 确定性删除定时任务（绕过 LLM，直连后端 task_scheduler）
#[tauri::command]
#[allow(dead_code)]
fn delete_scheduled_task(app: AppHandle, msg_id: String, task_id: String) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let sid = sidecar::current_session_id(&app)?;
    let payload = serde_json::json!({
        "type": "DELETE_SCHEDULED_TASK",
        "msg_id": msg_id,
        "task_id": task_id,
        "user_id": uid,
        "session_id": sid,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 请求 Dashboard 看板快照（用户级只读聚合，不依赖当前 session）
#[tauri::command]
#[allow(dead_code)]
fn request_dashboard(app: AppHandle, msg_id: String) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let payload = serde_json::json!({
        "type": "DASHBOARD_REQUEST",
        "msg_id": msg_id,
        "user_id": uid,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 全局搜索（命令面板 ⌘K）：会话标题 + 消息全文
#[tauri::command]
#[allow(dead_code)]
fn search_request(app: AppHandle, msg_id: String, query: String) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let sid = sidecar::current_session_id(&app)?;
    let payload = serde_json::json!({
        "type": "SEARCH",
        "msg_id": msg_id,
        "user_id": uid,
        "session_id": sid,
        "query": query,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 请求 Skill 列表
#[tauri::command]
fn request_skill_list(app: AppHandle, msg_id: String) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let sid = sidecar::current_session_id(&app)?;
    let payload = serde_json::json!({
        "type": "SKILL_LIST",
        "msg_id": msg_id,
        "user_id": uid,
        "session_id": sid,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 请求单个 Skill 详情
#[tauri::command]
fn request_skill_detail(app: AppHandle, msg_id: String, skill_name: String) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let sid = sidecar::current_session_id(&app)?;
    let payload = serde_json::json!({
        "type": "SKILL_GET",
        "msg_id": msg_id,
        "user_id": uid,
        "session_id": sid,
        "skill_name": skill_name,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 创建/更新 Skill
#[tauri::command]
fn save_skill(
    app: AppHandle,
    msg_id: String,
    skill_name: String,
    description: String,
    when_to_use: String,
    content: String,
    tags: Option<Vec<String>>,
) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let sid = sidecar::current_session_id(&app)?;
    let payload = serde_json::json!({
        "type": "SKILL_SAVE",
        "msg_id": msg_id,
        "user_id": uid,
        "session_id": sid,
        "skill_name": skill_name,
        "description": description,
        "when_to_use": when_to_use,
        "content": content,
        "tags": tags.unwrap_or_default(),
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 删除 Skill
#[tauri::command]
fn delete_skill(app: AppHandle, msg_id: String, skill_name: String) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let sid = sidecar::current_session_id(&app)?;
    let payload = serde_json::json!({
        "type": "SKILL_DELETE",
        "msg_id": msg_id,
        "user_id": uid,
        "session_id": sid,
        "skill_name": skill_name,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 导入 Skill
#[tauri::command]
fn import_skill(
    app: AppHandle,
    msg_id: String,
    content: String,
    format: String,
    overwrite: Option<bool>,
    source_path: Option<String>,
) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let sid = sidecar::current_session_id(&app)?;
    let mut payload = serde_json::json!({
        "type": "SKILL_IMPORT",
        "msg_id": msg_id,
        "user_id": uid,
        "session_id": sid,
        "content": content,
        "format": format,
    });
    if let Some(ow) = overwrite {
        payload["overwrite"] = serde_json::json!(ow);
    }
    if let Some(ref sp) = source_path {
        payload["source_path"] = serde_json::json!(sp);
    }
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 导出 Skill
#[tauri::command]
fn export_skill(
    app: AppHandle,
    msg_id: String,
    skill_name: String,
    format: String,
    target_path: Option<String>,
) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    let sid = sidecar::current_session_id(&app)?;
    let payload = serde_json::json!({
        "type": "SKILL_EXPORT",
        "msg_id": msg_id,
        "user_id": uid,
        "session_id": sid,
        "skill_name": skill_name,
        "format": format,
        "target_path": target_path,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

/// 停止指定 session 正在执行的 Agent 生成（用户点击停止按钮）
#[tauri::command]
fn stop_generation(
    app: AppHandle,
    msg_id: String,
    session_id: String,
) -> Result<(), String> {
    let uid = require_user_id(&app)?;
    // ★ 停止必须精确指向要停的 session，绝不能回退到全局「当前会话」单例：
    //   否则多会话并发时会停掉别的会话正在跑的长任务（跨会话误杀）。
    if session_id.is_empty() {
        return Err("session_id is required for stop_generation".into());
    }
    let sid = session_id;
    let payload = serde_json::json!({
        "type": "STOP_GENERATION",
        "msg_id": msg_id,
        "user_id": uid,
        "session_id": sid,
    });
    sidecar::write_to_sidecar(&app, &payload.to_string())
}

// ── 系统 Commands ─────────────────────────────────────────────────────────────

/// Tauri command：前端触发退出（统一走 lifecycle）
#[tauri::command]
fn quit_app(app: AppHandle) {
    lifecycle::request_shutdown(&app, "quit_command");
}

// ── App Builder ──────────────────────────────────────────────────────────────

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // ── 插件 ──────────────────────────────────────────────────────────
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        // ── 全局 State ────────────────────────────────────────────────────
        .manage(Mutex::new(SidecarState::new()))
        .manage(BackendToken::default())
        .manage(BackendModel::default())
        // ── 初始化钩子 ────────────────────────────────────────────────────
        .setup(|app| {
            let handle = app.handle().clone();

            // 系统托盘
            tray::setup_tray(&handle)?;

            // 跨平台 Ctrl+C / SIGTERM 处理器
            let signal_handle = handle.clone();
            ctrlc::set_handler(move || {
                eprintln!("[app] received termination signal");
                lifecycle::request_shutdown(&signal_handle, "signal");
            })
            .expect("failed to register Ctrl+C handler");

            // 窗口关闭 → 隐藏到托盘（而非退出）
            let window: WebviewWindow = app.get_webview_window("main").unwrap();
            window.on_window_event({
                let window = window.clone();
                move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        // 退出期间允许窗口正常关闭，避免 WebView2
                        // "Failed to unregister class Chrome_WidgetWin_0" 错误
                        if lifecycle::is_shutting_down() {
                            return; // 允许正常关闭
                        }
                        api.prevent_close();
                        let _ = window.hide();
                    }
                }
            });

            Ok(())
        })
        // ── Commands ──────────────────────────────────────────────────────
        .invoke_handler(tauri::generate_handler![
            get_auth_token,
            start_sidecar,
            check_llm_credentials,
            save_llm_credentials,
            get_provider_catalog,
            get_model_prices,
            send_message,
            send_hitl_decision,
            send_interaction_response,
            send_plan_approval_decision,
            send_ping,
            request_scheduled_tasks,
            delete_scheduled_task,
            request_dashboard,
            search_request,
            request_skill_list,
            request_skill_detail,
            save_skill,
            delete_skill,
            import_skill,
            export_skill,
            stop_generation,
            quit_app,
            set_current_session_id,
            send_session_ipc,
            auth::auth_notify_ready,
            auth::auth_update_token,
            auth::auth_get_token,
            auth::auth_get_username,
            auth::auth_logout,
            auth::auth_verify_token,
            auth::auth_local_status,
            auth::auth_local_register,
            auth::auth_local_login,
            workspace::open_workspace,
            workspace::get_recent_workspaces,
            workspace::get_current_workspace,
            pets::install_pet_urls,
            pets::fetch_pet_catalog,
            pets::list_pets,
            pets::remove_pet,
        ])
        // ── 运行时事件 ────────────────────────────────────────────────────
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let RunEvent::ExitRequested { .. } = event {
                lifecycle::request_shutdown(app, "system_exit");
            }
        });
}
