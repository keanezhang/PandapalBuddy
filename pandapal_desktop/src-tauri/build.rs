fn main() {
    // 将编译目标 triple 传递给 Rust 代码（sidecar.rs 用于定位可执行文件）
    let target = std::env::var("TARGET").unwrap_or_else(|_| "unknown".to_string());
    println!("cargo:rustc-env=TARGET_TRIPLE={}", target);

    tauri_build::build()
}
