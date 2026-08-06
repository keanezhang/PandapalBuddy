# PandaPal Desktop — 桌面客户端

> 基于 Tauri v2 + React + TypeScript 的 PandaPal 桌面客户端，Python Agent 以 PyInstaller sidecar 形式内嵌分发，面向 Windows 和 macOS。

## 功能特性

- **实时对话**：通过 Tauri IPC（invoke + event listen）与本地 Agent sidecar 通信
- **流式输出**：LLM 流式 token 展示（打字机效果）
- **HITL 审批**：工具调用需审批时自动弹出模态框
- **状态监控**：顶部状态栏显示 Agent 连接状态、运行阶段

## 架构

三层混合架构：**Tauri Shell（Rust，窗口/托盘/生命周期）** → **WebView（React + Vite，UI）** → **Python Sidecar（PyInstaller onedir，Agent 业务逻辑）**。Rust 在应用启动时自动拉起 sidecar，通过 stdin/stdout 进行 IPC，**不使用 WebSocket**。

| 层级 | 技术 |
|------|------|
| 前端 | React 18 + TypeScript + Vite + CSS Modules |
| 后端 | Tauri v2 (Rust) |
| 状态管理 | Zustand |
| IPC | @tauri-apps/api（invoke + event listen） |

## 项目结构

```
pandapal_desktop/
├── src/                         前端源码
│   ├── components/              UI 组件（ChatArea/、InputBar、HitlModal 等）
│   ├── pages/                   页面（ChatPage、LoginPage、SkillsPage 等）
│   ├── providers/BackendProvider.tsx   Tauri IPC 连接管理（消息分发中心）
│   ├── store/                   Zustand stores（chatStore / authStore 等）
│   └── types/api.ts             IPC 消息类型定义（与后端 codec 严格一致）
├── src-tauri/                   Rust 后端
│   ├── src/                     Rust 源码（commands、sidecar 管理）
│   ├── bin/                     sidecar 构建产物（git ignored，需本地构建）
│   ├── tauri.conf.json          跨平台共用配置
│   ├── tauri.windows.conf.json  Windows 专属配置（targets/resources）
│   └── tauri.macos.conf.json    macOS 专属配置
├── build_sidecar_windows.py     Windows sidecar 构建脚本
├── build_sidecar_macos.py       macOS sidecar 构建脚本
└── package.json
```

## 快速开始

### 前置条件

| 工具 | 版本要求 |
|------|---------|
| Rust | 最新 stable |
| Node.js | >= 18 |
| pnpm | `npm install -g pnpm` |
| Python | >= 3.12 |

- **Windows**：需安装 Microsoft C++ Build Tools（勾选 "Desktop development with C++"），并执行 `rustup default stable-msvc`；WebView2 运行时 Win11 自带，Win10 由安装包自动引导。
- **macOS**：需 Xcode Command Line Tools（`xcode-select --install`）；代码签名另需 Apple Developer ID。

### 开发模式

```bash
pnpm install
pnpm tauri:dev    # 自动构建 sidecar 并启动完整桌面应用
```

首次运行会编译 Rust 后端并打包 sidecar，耗时 2-5 分钟。

### 项目脚本

| 命令 | 说明 |
|------|------|
| `pnpm dev` | 仅启动 Vite 前端（浏览器预览） |
| `pnpm sidecar:build` | 构建 Python sidecar（自动识别当前平台） |
| `pnpm tauri:dev` | 构建 sidecar + 启动完整桌面应用（推荐） |
| `pnpm tauri build` | 打包安装包（Windows: `.exe` NSIS / macOS: `.app` + `.dmg`） |
| `pnpm typecheck` | TypeScript 类型检查 |

## 使用说明

1. **启动**：sidecar 由 Rust 自动拉起，无需手动启动后端或配置连接地址。
2. **通信链路**：前端 `invoke("send_message", ...)` → Rust 写入 sidecar stdin → sidecar stdout 输出 `IPC:{json}` 帧 → Rust `emit("backend-event", json)` 推回前端。
3. **HITL 审批**：Agent 调用需审批的工具时自动弹出模态框，点击「批准」或「拒绝」即可。

### IPC 命令（前端 → Rust）

| Command | 用途 |
|---------|------|
| `send_message` | 发送用户消息 |
| `send_hitl_decision` | HITL 审批决策 |
| `send_interaction_response` | 交互型工具回复 |
| `send_plan_approval_decision` | Plan Mode 审批 |
| `switch_situation` | 切换情境 |
| `stop_generation` | 停止当前生成 |
| `auth_verify_token` / `auth_logout` | 登录态校验 / 登出 |

**Python → 前端事件帧**：`REPLY_START` / `TOKEN` / `REPLY_END`、`TOOL_START` / `TOOL_END`、`HITL_REQUEST`、`PLAN_APPROVAL_REQUEST`、`ERROR` 等，完整清单见 `src/types/api.ts`（与 `pandapal/desktop_ipc/` 保持一致）。

## 打包发布

### Windows

```powershell
pnpm install
pnpm sidecar:build     # 等价于 python build_sidecar_windows.py
pnpm tauri build
```

产物：`src-tauri/target/release/bundle/nsis/PandaPal_<version>_x64-setup.exe`（约 100-180 MB）。

### macOS

```bash
pnpm install
pnpm sidecar:build     # 等价于 python build_sidecar_macos.py，自动检测 arm64 / x86_64
pnpm tauri build
```

产物：`src-tauri/target/release/bundle/` 下的 `macos/PandaPal.app` 和 `dmg/PandaPal_<version>_aarch64.dmg`（约 100-200 MB）。

> `tauri.macos.conf.json` 默认指向 `aarch64-apple-darwin`。在 Intel Mac 上打包需先把配置中的 resource 路径改为 `pandapal-sidecar-x86_64-apple-darwin`。

### 验证 sidecar（建议在 tauri build 前执行）

```bash
# Windows
cd src-tauri\bin\pandapal-sidecar-x86_64-pc-windows-msvc
.\pandapal-sidecar-x86_64-pc-windows-msvc.exe --user-id test --token test

# macOS
cd src-tauri/bin/pandapal-sidecar-aarch64-apple-darwin
./pandapal-sidecar-aarch64-apple-darwin --user-id test --token test
```

输出包含 `PANDAPAL_READY token=...` 即正常，`Ctrl+C` 退出。

### macOS 代码签名与公证（对外分发必需）

未签名的 .app 会被 Gatekeeper 拦截。在 `tauri.macos.conf.json` 中配置：

```json
{
  "bundle": {
    "macOS": {
      "signingIdentity": "Developer ID Application: Your Name (TEAM_ID)",
      "hardenedRuntime": true,
      "entitlements": "entitlements.plist"
    }
  }
}
```

启用 `hardenedRuntime` 必须配套 `src-tauri/entitlements.plist`，至少包含 `com.apple.security.cs.allow-unsigned-executable-memory` 和 `com.apple.security.cs.disable-library-validation` 两项（均为 true），否则 sidecar 子进程无法启动。

重新 `pnpm tauri build` 后公证 DMG：

```bash
# 凭据仅需首次配置
xcrun notarytool store-credentials "AC_PASSWORD" \
  --apple-id "your@apple.id" --team-id "TEAM_ID" --password "app-specific-password"

xcrun notarytool submit src-tauri/target/release/bundle/dmg/PandaPal_*_aarch64.dmg \
  --keychain-profile "AC_PASSWORD" --wait
xcrun stapler staple src-tauri/target/release/bundle/dmg/PandaPal_*_aarch64.dmg
```

### 发布检查清单

- [ ] 版本号三处同步：`package.json`、`src-tauri/tauri.conf.json`、`src-tauri/Cargo.toml`
- [ ] sidecar 单独启动能输出 `PANDAPAL_READY`
- [ ] `pnpm tauri dev` 全链路验证（登录 → 后端 ready → 发消息 → Agent 回复）
- [ ] 在干净环境中安装，验证 HITL、流式输出、托盘图标，并卸载重装确认配置读写正常

## 故障排查

### `resource path 'bin/pandapal-sidecar-...' doesn't exist`

先跑 `pnpm sidecar:build` 构建 sidecar；若构建失败检查脚本输出（多半是缺 hidden import）；再确认 `tauri.{platform}.conf.json` 中的 triple 与本机架构匹配。

### 运行时 `Sidecar binary not found for target 'xxx'`

前端能加载但发消息无反应。dev 模式回退到 `src-tauri/bin/`，production 走 `resource_dir/bin/`，确认打包产物的 `Resources/bin/` 内有 sidecar 子目录。

### sidecar 启动一闪而过

PyInstaller **严禁加 `--noconsole` / `--windowed`**（会关闭 stdin/stdout 导致 IPC 中断），脚本默认 `--console` 不要改；Tauri 在生产中通过 `CREATE_NO_WINDOW` 隐藏控制台窗口。

### Rust 编译失败

- `link.exe not found`（Windows）：MSVC 工具链缺失，执行 `rustup default stable-msvc` 并安装 VS Build Tools 的 C++ 工作负载。
- 其他情况：`rustup update` 后在 `src-tauri/` 下 `cargo clean` 重试。

### macOS 专属

- **`App is damaged and cannot be opened`**（未签名 .app）：临时执行 `xattr -cr /Applications/PandaPal.app`，长期方案是签名 + 公证。
- **Apple Silicon 产物在 Intel Mac 跑不了**：PyInstaller 产物为单架构，需分别在两种机器上构建发布两个 DMG。

### 通信异常排查顺序

1. 确认 Python ≥ 3.12 且 `src-tauri/bin/` 下存在 sidecar 产物
2. 确认 `pnpm sidecar:build` 成功
3. 查看 `pnpm tauri:dev` 控制台输出，确认 sidecar 进程已拉起无报错
4. 前端 F12 → Console，检查 `backend-event` 监听是否正常

## 参考链接

- Tauri v2 打包文档：https://tauri.app/v2/guides/build/
- PyInstaller onedir 模式：https://pyinstaller.org/en/stable/operating-mode.html
- Apple 公证流程：https://developer.apple.com/documentation/security/notarizing_macos_software_before_distribution

## 许可证

MIT
