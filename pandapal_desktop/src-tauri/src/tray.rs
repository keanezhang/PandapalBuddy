//! pandapal-desktop/src-tauri/src/tray.rs
//!
//! 职责：
//! - 构建系统托盘菜单（显示/隐藏窗口、退出）
//! - 处理托盘图标点击（左键 → 显示窗口；右键 → 菜单）
//! - 退出统一走 lifecycle::request_shutdown

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, Runtime,
};

use crate::lifecycle;

/// 初始化系统托盘。在 app setup 钩子中调用。
pub fn setup_tray<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let show_item = MenuItem::with_id(app, "show", "显示窗口", true, None::<&str>)?;
    let hide_item = MenuItem::with_id(app, "hide", "隐藏到托盘", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(app)?;
    let quit_item = MenuItem::with_id(app, "quit", "退出 PandaPal", true, None::<&str>)?;

    let menu = Menu::with_items(app, &[&show_item, &hide_item, &sep, &quit_item])?;

    TrayIconBuilder::with_id("main-tray")
        .icon(app.default_window_icon().unwrap().clone())
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| {
            handle_menu_event(app, event.id.as_ref());
        })
        .on_tray_icon_event(|tray, event| {
            // 左键单击 → 显示/聚焦主窗口
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                ..
            } = event
            {
                let app = tray.app_handle();
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
        })
        .build(app)?;

    Ok(())
}

fn handle_menu_event<R: Runtime>(app: &AppHandle<R>, id: &str) {
    match id {
        "show" => {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }
        "hide" => {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.hide();
            }
        }
        "quit" => {
            lifecycle::request_shutdown(app, "tray_quit");
        }
        _ => {}
    }
}
