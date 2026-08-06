# 贡献指南

感谢你愿意为 PandaPal Buddy 贡献力量！在提交代码前，请阅读以下约定。

## 环境准备

- Python ≥ 3.12（SDK 与后端）
- Node.js ≥ 18 + pnpm（桌面端，见 `pandapal_desktop/package.json`）
- Rust 工具链（Tauri 桌面端，见 `pandapal_desktop/src-tauri/Cargo.toml`）

```bash
# 安装 SDK 及开发依赖
pip install -e ".[dev]"
```

## 代码规范

- 使用 [ruff](https://github.com/astral-sh/ruff) 做 lint（配置在 `pyproject.toml`）：
  ```bash
  ruff check .
  ```
- 行宽 100，Python ≥ 3.12 语法
- 遵循项目核心设计原则（见 `PANDAPAL.md` §1.3）：
  - **HC1**：`Identity` 运行时不可修改
  - **HC4**：`AuditLog` 不可关闭，任何代码路径不得绕过审计写入
  - **O3**：`Agent.run()` 永不向外抛异常，异常必须内部转换为 `AgentResult`
  - **S3**：不得继承 `Identity`

## 测试

```bash
pytest                       # 运行全部测试（pandapal/、pandaren/、scripts/）
```

- 新增功能必须有对应测试（`pytest` + `pytest-asyncio`，asyncio 模式为 auto）
- 涉及 IPC 消息类型变更时，`pandapal/desktop_ipc/` 与 `pandapal_desktop/src/types/api.ts` 必须同步更新
- 涉及 `session_id` 的改动，请先阅读 `docs/design/session-id-契约.md`

## 提交约定

- 提交信息用一句话描述变更，如 `fix: 修复 HITL 暂停后会话恢复丢失的问题`
- 每个 PR 聚焦一个主题，便于 review 与回滚

## PR 流程

1. Fork 本仓库并创建特性分支
2. 提交代码（含测试）
3. 确保 `ruff check .` 与 `pytest` 通过
4. 发起 Pull Request，描述变更动机与影响面

## 目录结构速查

| 你想改什么 | 从哪里开始 |
|-----------|-----------|
| SDK 引擎 / 新增 Tool | `pandaren/`（参考 `pandaren/tools/` 现有实现） |
| 运行时后端 / 消息路由 | `pandapal/` |
| 桌面端页面 / IPC 消息 | `pandapal_desktop/` |
| 云端 Relay / 企微 / 音箱 | `pandapal_relay/` |
