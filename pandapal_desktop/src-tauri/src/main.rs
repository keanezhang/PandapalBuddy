//! pandapal-desktop/src-tauri/src/main.rs

// 防止 Windows 上弹出额外的控制台窗口
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    pandapal_desktop_lib::run()
}
