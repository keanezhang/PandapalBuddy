//! pandapal-desktop/src-tauri/src/lifecycle.rs
//!
//! 统一退出管理器。
//!
//! 所有退出触发点（quit_app、tray、ctrlc、ExitRequested）统一调用 request_shutdown，
//! 保证幂等性（只执行一次 shutdown）、避免分散的退出逻辑。

use std::sync::atomic::{AtomicBool, Ordering};
use tauri::{AppHandle, Runtime};

use crate::sidecar;

/// 全局退出标记，防止重复 shutdown
static SHUTTING_DOWN: AtomicBool = AtomicBool::new(false);

/// 查询是否正在退出。用于窗口 `CloseRequested` 处理器判断
/// 是否允许窗口正常关闭（而非 hide-to-tray）。
pub fn is_shutting_down() -> bool {
    SHUTTING_DOWN.load(Ordering::SeqCst)
}

/// 唯一的应用退出入口。
///
/// 所有退出触发点（quit_app、tray、ctrlc、ExitRequested）统一调用此函数。
/// 幂等：多次调用只执行一次实际 shutdown。
pub fn request_shutdown<R: Runtime>(app: &AppHandle<R>, reason: &str) {
    if SHUTTING_DOWN.swap(true, Ordering::SeqCst) {
        eprintln!("[lifecycle] shutdown already in progress, skip (reason: {})", reason);
        return;
    }

    eprintln!("[lifecycle] shutting down (reason: {})", reason);
    sidecar::shutdown_sidecar(app);
    app.exit(0);
}

/// 仅关闭 sidecar 但不退出 App（用于 auth_logout 场景）。
pub fn kill_sidecar_only<R: Runtime>(app: &AppHandle<R>) {
    sidecar::shutdown_sidecar(app);
}

/// 重置 shutdown 标记（用于测试或 sidecar 重启场景）。
///
/// auth_logout 后用户可能重新登录，此时需要允许再次 shutdown。
pub fn reset_shutdown_flag() {
    SHUTTING_DOWN.store(false, Ordering::SeqCst);
}
