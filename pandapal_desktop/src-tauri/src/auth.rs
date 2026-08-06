//! pandapal-desktop/src-tauri/src/auth.rs
//!
//! 认证模块：提供 Tauri commands 管理 auth 状态。
//!
//! 串行启动架构（工作区门控版）：
//!   前端 fetch("https://relay/auth/login") → 直接得到 token + user_id
//!   前端 invoke("auth_notify_ready", {token, user_id, username})
//!     → Rust 仅保存凭据到 store（**不再** spawn sidecar）
//!   前端展示「打开文件夹」→ 用户选定后 invoke("open_workspace", {path})
//!     → Rust 携带 --user-id / --token / --workdir 启动 sidecar（见 workspace.rs）
//!     → Python stdout → "PANDAPAL_READY" → Rust → 前端 listen("backend-ready")
//!
//! 为何拆分：Agent 文件工具的根目录只能由用户显式选择、不做任何探测，
//! 因此「登录」与「选工作区并启动 Agent」是两个独立步骤。
//!
//! Token 持久化：使用 tauri-plugin-store。

use serde::Serialize;
use tauri::AppHandle;
use tauri_plugin_store::StoreExt;

use crate::lifecycle;
use crate::sidecar;

/// Store 文件名
const AUTH_STORE_FILE: &str = "auth_store.json";
/// Store 中 token 的 key
const TOKEN_KEY: &str = "jwt_token";
/// Store 中 username 的 key
const USERNAME_KEY: &str = "username";
/// Store 中 user_id 的 key
const USER_ID_KEY: &str = "user_id";
/// Store 中 auth_mode 的 key（"local" / "cloud"，缺省视为 cloud）
const AUTH_MODE_KEY: &str = "auth_mode";
/// 本地账号文件（与 auth_store.json 同目录，单账号）
const LOCAL_ACCOUNT_FILE: &str = "local_account.json";
/// local_account.json 内字段
const LOCAL_USERNAME_KEY: &str = "username";
const LOCAL_PASSWORD_HASH_KEY: &str = "password_hash";
const LOCAL_USER_ID_KEY: &str = "user_id";

// ── 响应结构（返回给前端） ────────────────────────────────────────────────────

#[derive(Serialize, Clone)]
pub struct AuthCommandResult {
    pub success: bool,
    pub user_id: String,
    pub username: String,
    /// 认证模式："local"（本地账号）/ "cloud"（云端账号），缺省视为 cloud
    pub mode: String,
}

// ── Tauri Commands ────────────────────────────────────────────────────────────

/// 前端 HTTP 认证成功后调用：仅保存凭据到 store。
///
/// 不再在此处启动 sidecar —— 启动发生在用户「打开文件夹」后的 open_workspace。
#[tauri::command]
pub fn auth_notify_ready(
    app: AppHandle,
    token: String,
    user_id: String,
    username: String,
) -> Result<AuthCommandResult, String> {
    save_auth_to_store(&app, &token, &username, &user_id, "cloud")?;

    Ok(AuthCommandResult {
        success: true,
        user_id,
        username,
        mode: "cloud".to_string(),
    })
}

/// 自动登录：从 store 读取已保存的凭据（仅校验，不启动 sidecar）。
///
/// 校验通过后，前端负责后续「恢复上次工作区 / 展示打开文件夹」，
/// 最终由 open_workspace 携带 --workdir 启动 sidecar。
#[tauri::command]
pub fn auth_verify_token(app: AppHandle) -> Result<AuthCommandResult, String> {
    let store = app
        .store(AUTH_STORE_FILE)
        .map_err(|e| format!("打开 store 失败: {}", e))?;
    let username = store
        .get(USERNAME_KEY)
        .and_then(|v| v.as_str().map(String::from))
        .unwrap_or_default();
    let mode = store
        .get(AUTH_MODE_KEY)
        .and_then(|v| v.as_str().map(String::from))
        .unwrap_or_else(|| "cloud".to_string());

    match read_credentials(&app) {
        Some((uid, _token)) => Ok(AuthCommandResult {
            success: true,
            user_id: uid,
            username,
            mode,
        }),
        None => Ok(AuthCommandResult {
            success: false,
            user_id: String::new(),
            username: String::new(),
            mode,
        }),
    }
}

/// 读取已保存的登录凭据 (user_id, token)。
///
/// 供 workspace::open_workspace 在启动 sidecar 时取用。
/// 返回 None 表示尚未登录（凭据缺失或为空）。
///
/// 模式化判定：本地模式（auth_mode=="local"）token 允许为空（不连服务器，
/// JWT 仅云端 Gateway 鉴权用）；云端模式（auth_mode 缺省视为 cloud）token
/// 必须非空，缺失视为凭据损坏/未登录。
pub fn read_credentials(app: &AppHandle) -> Option<(String, String)> {
    let store = app.store(AUTH_STORE_FILE).ok()?;
    let user_id = store
        .get(USER_ID_KEY)
        .and_then(|v| v.as_str().map(String::from))?;
    if user_id.is_empty() {
        return None;
    }
    let mode = store
        .get(AUTH_MODE_KEY)
        .and_then(|v| v.as_str().map(String::from))
        .unwrap_or_default();
    let token = store
        .get(TOKEN_KEY)
        .and_then(|v| v.as_str().map(String::from))
        .unwrap_or_default();
    if mode == "local" {
        return Some((user_id, token));
    }
    if token.is_empty() {
        return None;
    }
    Some((user_id, token))
}

/// 获取已存储的 token
#[tauri::command]
pub fn auth_get_token(app: AppHandle) -> Result<Option<String>, String> {
    let store = app
        .store(AUTH_STORE_FILE)
        .map_err(|e| format!("打开 store 失败: {}", e))?;

    let token = store.get(TOKEN_KEY).and_then(|v| v.as_str().map(String::from));
    Ok(token)
}

/// 获取已存储的用户名
#[tauri::command]
pub fn auth_get_username(app: AppHandle) -> Result<Option<String>, String> {
    let store = app
        .store(AUTH_STORE_FILE)
        .map_err(|e| format!("打开 store 失败: {}", e))?;

    let username = store.get(USERNAME_KEY).and_then(|v| v.as_str().map(String::from));
    Ok(username)
}

/// JWT 自动续期后回写新 token。
///
/// 触发链路：Python Gateway refresh 成功 → IPC AUTH_TOKEN_REFRESHED →
/// 前端 invoke("auth_update_token", { token })。
/// 作用：
///   1. 回写 auth_store.json（宽限期锚点持续前移，下次冷启动不再过期）；
///   2. 同步内存 BackendToken（与 sidecar 保持同一份 token）。
/// 仅更新 token，不动 username / user_id（refresh 换发的是同用户新 JWT）。
#[tauri::command]
pub fn auth_update_token(app: AppHandle, token: String) -> Result<(), String> {
    if token.is_empty() {
        return Err("token 不能为空".to_string());
    }

    let store = app
        .store(AUTH_STORE_FILE)
        .map_err(|e| format!("打开 store 失败: {}", e))?;
    store.set(TOKEN_KEY, serde_json::json!(token));
    store
        .save()
        .map_err(|e| format!("保存 store 失败: {}", e))?;

    // 同步内存缓存（BackendToken），保证 warm-start 探测与后续读取一致
    sidecar::set_backend_token(&app, &token);

    Ok(())
}

/// 登出：关闭 sidecar 并清除 store 中的凭据
#[tauri::command]
pub fn auth_logout(app: AppHandle) -> Result<(), String> {
    // 1. 仅关闭 sidecar，不退出 App
    lifecycle::kill_sidecar_only(&app);
    // 2. 清除内存 token
    sidecar::clear_backend_token(&app);
    // 3. 重置 shutdown 标记（允许后续重新登录后再次正常退出）
    lifecycle::reset_shutdown_flag();

    let store = app
        .store(AUTH_STORE_FILE)
        .map_err(|e| format!("打开 store 失败: {}", e))?;

    store.delete(TOKEN_KEY);
    store.delete(USERNAME_KEY);
    store.delete(USER_ID_KEY);
    store.delete(AUTH_MODE_KEY);
    store
        .save()
        .map_err(|e| format!("保存 store 失败: {}", e))?;

    Ok(())
}

/// 保存登录凭据到 store（auth_mode: "cloud" / "local"）
pub fn save_auth_to_store(
    app: &AppHandle,
    token: &str,
    username: &str,
    user_id: &str,
    auth_mode: &str,
) -> Result<(), String> {
    let store = app
        .store(AUTH_STORE_FILE)
        .map_err(|e| format!("打开 store 失败: {}", e))?;

    store.set(TOKEN_KEY, serde_json::json!(token));
    store.set(USERNAME_KEY, serde_json::json!(username));
    store.set(USER_ID_KEY, serde_json::json!(user_id));
    store.set(AUTH_MODE_KEY, serde_json::json!(auth_mode));
    store
        .save()
        .map_err(|e| format!("保存 store 失败: {}", e))?;

    Ok(())
}

// ── 本地账号（local_account.json，单账号，bcrypt 哈希） ─────────────────────

/// 本地账号状态（前端判断显示「创建」还是「登录」表单）
#[derive(Serialize, Clone)]
pub struct LocalAccountStatus {
    pub registered: bool,
    pub username: String,
}

/// 读取本地账号：(username, password_hash, user_id)。缺失/损坏返回 None。
fn read_local_account(app: &AppHandle) -> Option<(String, String, String)> {
    let store = app.store(LOCAL_ACCOUNT_FILE).ok()?;
    let username = store
        .get(LOCAL_USERNAME_KEY)
        .and_then(|v| v.as_str().map(String::from))?;
    let password_hash = store
        .get(LOCAL_PASSWORD_HASH_KEY)
        .and_then(|v| v.as_str().map(String::from))?;
    let user_id = store
        .get(LOCAL_USER_ID_KEY)
        .and_then(|v| v.as_str().map(String::from))?;
    if username.is_empty() || password_hash.is_empty() || user_id.is_empty() {
        return None;
    }
    Some((username, password_hash, user_id))
}

/// 写入本地账号（明文密码绝不落盘，只存 bcrypt 哈希）。
fn write_local_account(
    app: &AppHandle,
    username: &str,
    password_hash: &str,
    user_id: &str,
) -> Result<(), String> {
    let store = app
        .store(LOCAL_ACCOUNT_FILE)
        .map_err(|e| format!("打开 store 失败: {}", e))?;

    store.set(LOCAL_USERNAME_KEY, serde_json::json!(username));
    store.set(LOCAL_PASSWORD_HASH_KEY, serde_json::json!(password_hash));
    store.set(LOCAL_USER_ID_KEY, serde_json::json!(user_id));
    store
        .save()
        .map_err(|e| format!("保存 store 失败: {}", e))?;

    Ok(())
}

/// 查询本地账号是否已注册（前端据此显示「创建本地账号」或「本地登录」）。
#[tauri::command]
pub fn auth_local_status(app: AppHandle) -> Result<LocalAccountStatus, String> {
    Ok(match read_local_account(&app) {
        Some((username, _, _)) => LocalAccountStatus {
            registered: true,
            username,
        },
        None => LocalAccountStatus {
            registered: false,
            username: String::new(),
        },
    })
}

/// 创建本地账号（单账号：已注册即报错）并自动登录。
///
/// - 密码长度 ≥ 6（对齐云端 relay `_validate_password` 最低要求）；
/// - bcrypt cost=12 哈希存储（与云端同策略），明文不落盘/不落日志；
/// - user_id = local_<uuid4>，写会话身份到 auth_store.json（auth_mode="local"，
///   token 为空串 → sidecar 以 offline 模式运行）。
#[tauri::command]
pub fn auth_local_register(
    app: AppHandle,
    username: String,
    password: String,
) -> Result<AuthCommandResult, String> {
    let username = username.trim().to_string();
    if username.is_empty() {
        return Err("用户名不能为空".to_string());
    }
    if password.len() < 6 {
        return Err("密码至少需要 6 位".to_string());
    }
    if read_local_account(&app).is_some() {
        return Err("本地账号已存在，请直接登录".to_string());
    }

    let password_hash = bcrypt::hash(&password, bcrypt::DEFAULT_COST)
        .map_err(|e| format!("密码加密失败: {}", e))?;
    let user_id = format!("local_{}", uuid::Uuid::new_v4());

    write_local_account(&app, &username, &password_hash, &user_id)?;
    save_auth_to_store(&app, "", &username, &user_id, "local")?;

    Ok(AuthCommandResult {
        success: true,
        user_id,
        username,
        mode: "local".to_string(),
    })
}

/// 本地账号登录：bcrypt 校验通过后建立本地会话。
///
/// 恒时比对：用户名不匹配时也执行一次 bcrypt verify（对真实 hash 做必失败的
/// 比对），避免攻击者通过响应耗时枚举本地账号是否存在（对齐云端 BL2）。
#[tauri::command]
pub fn auth_local_login(
    app: AppHandle,
    username: String,
    password: String,
) -> Result<AuthCommandResult, String> {
    let (stored_username, password_hash, user_id) = read_local_account(&app)
        .ok_or_else(|| "尚未创建本地账号".to_string())?;

    let verified = if username.trim() == stored_username {
        bcrypt::verify(&password, &password_hash).unwrap_or(false)
    } else {
        // 恒时：真实 hash 与错误用户名必不匹配，但耗时与成功路径一致
        let _ = bcrypt::verify(&password, &password_hash);
        false
    };
    if !verified {
        return Err("用户名或密码错误".to_string());
    }

    save_auth_to_store(&app, "", &stored_username, &user_id, "local")?;
    Ok(AuthCommandResult {
        success: true,
        user_id,
        username: stored_username,
        mode: "local".to_string(),
    })
}
