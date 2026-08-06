# PandaPal Buddy

> **企业级 Python Agent 构建框架 —— 让 Agent 的每一步都「看得见、管得住」**

[English](README.md) | 简体中文

PandaPal Buddy 是一个面向生产环境的 Agent 开发框架，覆盖 **核心引擎 → 运行时后端 → 桌面客户端 → 云端接入** 的完整链路。它把可观测性、权限控制、人工审批（HITL）内建到 Agent 循环里，而不是事后外挂。

仓库包含 **四个子项目**，分层协作：

| 子项目 | 层级 | 说明 |
|--------|------|------|
| [`pandaren/`](pandaren/) | SDK 核心库 | 引擎、安全、记忆、观测。无上下游依赖 |
| [`pandapal/`](pandapal/) | 运行时后端 | Agent 应用层，IoC 容器 + 声明式子系统，连接多渠道 |
| [`pandapal_desktop/`](pandapal_desktop/) | 桌面前端 | Tauri v2 + React 18，通过 stdin/stdout IPC 与后端通信 |
| [`pandapal_relay/`](pandapal_relay/) | 云端 Relay（可选） | FastAPI 服务，接入企业微信、XiaoZhi 智能音箱 |

```
┌─────────────────────────────────────────────────────────────┐
│  pandapal_desktop            Tauri v2 桌面前端              │
│  (React + TypeScript)        用户交互、流式打字机、HITL审批  │
└──────────────────────────┬──────────────────────────────────┘
                           │ stdin/stdout (IPC JSON Lines)
┌──────────────────────────▼──────────────────────────────────┐
│  pandapal                    Agent 运行时后端 (本地 sidecar) │
│  (Python)                    消息路由、Agent调度、HITL管理   │
│                              依赖 pandaren 引擎              │
└──────────────┬───────────────────────────┬──────────────────┘
               │                           │ WebSocket
┌──────────────▼──────────┐  ┌─────────────▼──────────────────┐
│  pandaren                │  │  pandapal_relay               │
│  Agent SDK 核心库 (Python)│  │  云端 Relay Server (FastAPI)   │
│  引擎/安全/记忆/观测      │  │  企微接入 + XiaoZhi智能音箱     │
└──────────────────────────┘  └────────────────────────────────┘
```

---

## 🎯 为什么是 PandaPal Buddy？

大多数 Agent 项目只给你一个库或一个 demo。PandaPal Buddy 给你**一台今天就能开箱即用的桌面 AI 智能助手**，并把支撑它的每一层源码都摊开在阳光下。我们始终坚守一个信念：

> **一个 Agent 只有当每一步都看得见、每一个危险动作都管得住时，才值得信任。**

### 1️⃣ 桌面级 AI 智能助手，不只是一套框架

**提炼出的三大核心能力：**

- **🧭 自主执行LOOP**——一句话进去，多步任务做完：任务编排、多 Agent 协作、复杂任务规划、自定义 Skill，把一个指令变成一整个工作流
- **👁️ 全程可视**——Dashboard 看板一览预算、会话与 Agent 活动；AI 修改 diff 展示每一处文件改动；推理过程实时流出。没有一步在暗处发生
- **🛑 关键处人控**——高危操作 HITL 审批暂停到你做决定；Plan Mode 让任何代码执行前先过审批；任务安排让它在你睡觉时持续干活

主流能力借鉴 Claude Code codex  workbuddy等一线 AI 编码助手，**编码、办公双场景开箱即用**——配备完整桌面 UI（Tauri v2 + React），而不是一个光秃秃的命令行。

### 2️⃣ 全栈源码开放，零黑盒

从云端服务器到引擎内核，每一层都是开源的：

| 层级 | 项目 | 你能拿到什么 |
|------|------|-------------|
| 云端服务器 | [`pandapal_relay/`](pandapal_relay/) | FastAPI Relay：企微 + 智能音箱接入、账号认证系统 |
| 运行时后端 | [`pandapal/`](pandapal/) | Agent 应用层：IoC 容器、路由、调度、HITL 管理 |
| SDK 核心 | [`pandaren/`](pandaren/) | 引擎本体：8-Phase ReAct、安全、记忆、可观测性 |
| 桌面客户端 | [`pandapal_desktop/`](pandapal_desktop/) | Tauri v2 + React GUI，把 Agent 全生命周期实时呈现在你眼前 |

没有任何闭源部分：通信协议、引擎内核、界面——读它、改它、自己跑起来。

### 3️⃣ 看得见、管得住——可观测性不是外挂，是产品

可观测性**内建于循环**，而不是事后外挂的插件。**Dashboard** 把 Agent 的运行轨迹变成可回放的看板（Token、工具、费用、审批），引擎在代码层面强制其余约束：

- `AuditLog` **任何代码路径都不可关闭**——每一次运行都留下完整审计轨迹
- `Identity` 物理不可变（`__slots__` + `__setattr__`）——运行时无法提权
- 熔断阈值触发，循环立即终止
- 12 条 `session_id` 契约红线——跨会话数据污染在构造上就不可能发生

**速览** —— 4 个开源子项目 · 8-Phase ReAct 循环 · 17 种流式事件 · 46 种归一化事件 · 21 个生命周期 Hook · 11 个可替换扩展点 · 四大观测支柱

---

## ✨ 核心特性

**引擎核心（`pandaren/`）**

- **8-Phase ReAct 循环**：上下文预测 → 消息构建 → LLM 调用 → 输出解析 → 工具选择 → 工具执行 → 结果收集 → 决策，执行内核统一收敛（`RunCoreMixin._run_stream_core()`）
- **权限与安全内建**：`Identity` 物理不可变（`__slots__` 阻断修改）、`AuditLog` 不可关闭、权限守卫（PermissionGuard）、熔断器
- **人工审批（HITL）**：高危操作实时暂停、人工决策、恢复续跑，全流程可审计；桌面端、企业微信（经 Relay）多渠道可用
- **观测四支柱**：AuditLog / Tracer / Metrics / Logger 横切全链路，17 种流式事件、21 个生命周期 Hook
- **三层记忆**：短期/长期/工作记忆 + 四层压缩管线，Token 预算精确控制
- **多 Agent 委派**：SubAgent 注册表 + 蓝图，复杂任务拆解执行
- **11 个可替换扩展点**：记忆后端、观测后端、压缩策略、Token 估算器等全部 Protocol 化，可插拔
- **支持Plan、AskUser、HITL**等多种人与AI的互动方式
- **46种流式事件**完美支持
- **支持主流模型** Anthropic  deepseek  dashscope  Volcengine

**运行时与桌面（`pandapal/`、`pandapal_desktop/`、`pandapal_relay/`）**

- **Dashboard 看板**——预算、会话、Agent 活动一览无余
- **AI 修改 diff 展示**——每次文件改动都以 Monaco 内联 diff 呈现，不藏任何细节
- **任务安排**——cron / 事件 / 手动三种触发，你睡觉它干活
- **预算额度管控**——按 provider 分账，撞额度即停
- **企微 / 智能音箱渠道（印象渠道还在开发中）**——企微内审批卡片，或对着 XiaoZhi 音箱对话
- **支持petdex桌宠**——Agent 活动 → 宠物动画反馈

---

## 🗺️ 能力地图

> ★ = 我们认为差异化最明显的核心能力。

### `pandaren/` — 引擎核心（SDK）

| 能力 | 说明 |
|---|---|
| ★ 8-Phase ReAct 循环 | 统一执行内核：上下文预测 → 消息构建 → LLM 调用 → 输出解析 → 工具选择 → 工具执行 → 结果收集 → 决策 |
| ★ Identity 物理不可变 | `__slots__` + `__setattr__` + `__delattr__` 三重拦截一切运行时修改——无法提权 |
| ★ TrustLevel 信任分级 | EXTERNAL / SUB_AGENT / ORCHESTRATOR——子 Agent 永远无法高于编排者 |
| ★ AuditLog 不可关闭 | 21 种审计事件类型，任何代码路径都无法绕过审计轨迹 |
| 权限守卫 PermissionGuard | 按敏感度（LOW → CRITICAL）拦截高危操作 |
| 熔断器 + Harness | 阈值触发即终止循环；步数 / 超时 / 上下文预算三重护栏 |
| HITLController | 高危操作暂停 → 人工决策 → 恢复续跑 |
| 多 Provider LLM | OpenAI 兼容协议 + 通义 / 豆包 / DeepSeek 插件；LLMRouter 路由 |
| 两层工具分级 | ALWAYS（≤15 个常驻）/ DEFERRED（按需加载）——控制 Token 开销 |
| ★ Skill 技能注入 | 纯 Markdown 定义技能，自动触发控制 |
| ★ 三层记忆 | 短期 / 长期 / 工作记忆 + 4 阶段压缩管线 + Token 预算精确控制 |
| ★ 四大观测支柱 | AuditLog / Tracer（8 种 Span）/ Metrics / Logger；4 套后端可换 |
| ★ 21 个生命周期 Hook | 从 run 到 skill，全生命周期可扩展 |
| ★ 11 个可替换扩展点 | 记忆后端、观测后端、压缩策略、Token 估算器……全部 Protocol 化 |
| 17 种流式事件 | 含 REASONING_TOKEN（推理过程可见）、PLAN_APPROVAL_REQUESTED |
| Plan Manager | 引擎级「规划 → 审批 → 执行」 |
| 多 Agent 委派 | SubAgent 注册表 + 蓝图 |
| 取消机制 | CancelToken——随时中止任何运行 |
| AgentBuilder Fluent API | 16 个链式方法，约 20 行构建一个全副装备的 Agent |

### `pandapal/` — 运行时后端

| 能力 | 说明 |
|---|---|
| ★ IoC 容器 | 17 个声明式子系统，自动拓扑排序 + 依赖注入；单个子系统失败不阻塞整体 |
| 消息路由 | 解析 / 去重 / 路由，9 种路由词汇 |
| 流式执行引擎 | AgentExecutor——run_stream 实时流出每个事件 |
| ★ 会话并发池 | 多会话并发 + queued/started/released 三态 + 驱逐机制 |
| ★ HITL 状态唯一 Owner | HITLBridge 防竞态；ask_user 问卷独立于审批 |
| ★ 跨渠道广播 | 一个事件 → 桌面 / 企微 / 音箱，每渠道可配策略 |
| ★ 46 种归一化事件 | 会话级 / 全局级显式二分 |
| ★ session_id 契约 | 12 条红线——跨会话数据污染在构造上不可能 |
| ★ 预算额度 | 按 provider 分账，撞额度即停（不是事后算账） |
| 持久化 | SQLite + Markdown 双后端；10+ Repository（审计 / 会话 / 任务 / 工作记忆……） |
| 定时任务 | cron / 事件 / 手动三种触发 |
| 看板聚合 | 喂给桌面 Dashboard |
| Agent 任务工具 | 任务拆解 / 进度上报 / 结果回收 |
| Web 搜索与抓取 | 内置 web 工具 |
| 统一降级通道 | 每次降级都留痕（log + Metrics）——绝不静默 |
| 后台任务隔离 | spawn_background——故障不影响主流程 |

### `pandapal_desktop/` — 桌面客户端（Tauri + React）

| 能力 | 说明 |
|---|---|
| ★ AI 修改 diff 展示 | 每次文件改动都以 Monaco 内联 diff 呈现（独立子工程） |
| ★ 流式打字机 + 推理过程 | Token 与 REASONING_TOKEN 实时渲染 |
| ★ 工具调用可视化 | 每个工具专属渲染器（bash / 编辑 / 读写 / 网页抓取……） |
| ★ 桌宠 | Agent 活动 → 宠物动画反馈 |
| HITL / Plan / ask_user 三类弹窗 | 三种暂停全覆盖 |
| 文件渲染器 | HTML / 图片 / Markdown / PDF / 表格 / 日志 内联预览 |
| Dashboard 看板页 | 预算 / 会话 / Agent 活动聚合 |
| 技能管理页 + 独立编辑器 | 可视化 Skill 增删改 |
| 任务安排页 | 可视化 cron 管理 |
| BYOK 凭据 | 自带 Key + 模型配置向导 |
| ⌘K 命令面板 | 全局搜索 |
| 文件树 / 工作区 | 文件资源管理器 + 工作区切换 |
| 会话管理 | 分组 / 收藏 / 历史 / 并发控制 |
| 22 个 Zustand store | 状态分域清晰 |

### `pandapal_relay/` — 云端 Relay

| 能力 | 说明 |
|---|---|
| ★ 企微接入 | 消息回调 + 企微内 HITL 审批卡片 |
| ★ XiaoZhi 智能音箱 | 语音交互：Opus 缓冲 + ASR + TTS（后端可换） |
| 可靠 WebSocket 通道 | JWT 鉴权、50 条断连缓冲、ACK 可靠投递、心跳保活 |
| 账号系统 | 注册即登录、JWT、防枚举 / 锁定、refresh 宽限 |
| Fail-Fast 配置 | 配置不全会拒绝启动，不带病上线 |

---

## 📦 1. `pandaren/` — Agent SDK 核心库

一切的基石：纯 Python 的面向生产环境的 Agent 引擎。**无上下游依赖**，可通过 `pip install pandapal-buddy` 独立使用。

### 四层分层架构

```
Layer 4: engine/         AgentLoop (8-Phase ReAct)、消息构建、输出解析、流式事件
Layer 3: behavior/       PermissionGuard、HITLController、Harness（运行时保护）
Layer 2: capability/     llm/ + tool/ + skill/ + memory/
Layer 1: identity/       Identity、Permission、TrustLevel（不可变地基）

横切: observability/     AuditLog、Tracer、Metrics、Logger（四大观测支柱）
横切: hooks.py           AgentHooks（21 个统一生命周期扩展点）
```

依赖方向严格单向：`engine → behavior → capability → identity`

### 快速开始

**环境要求**：Python ≥ 3.12

```bash
pip install pandapal-buddy
# 可选扩展
pip install pandapal-buddy[all]   # 全功能（PDF/图片/回收站/校验/tokenizer/测试）
```

**最小 Agent 示例**：

```python
from pandaren.builder import AgentBuilder
from pandaren.llm import OpenAICompatibleClient

agent = (
    AgentBuilder()
    .identity(agent_id="my_agent", agent_name="助手")
    .llm(client=OpenAICompatibleClient.for_openai(api_key="sk-..."))
    .llm_settings(temperature=0.7, max_tokens=4096)
    .tools([search_web, calc])          # 注册工具（两层分级 ALWAYS/DEFERRED）
    .skills([domain_knowledge])         # 注入领域知识
    .system_prompt("你是一个助手...")
    .behavior(max_steps=30, total_timeout=300.0)
    .observability()                    # 默认审计 + 追踪
    .build()
)

result = await agent.run("帮我查一下天气", session_id="session-001")
# result: AgentResult —— 永不抛异常，所有异常内部转换为结果（O3）
```

**模块速查**：

| 模块 | 核心类 | 职责 |
|------|--------|------|
| `pandaren/identity/` | `Identity`, `Permission`, `TrustLevel` | 不可变身份声明、权限集合 |
| `pandaren/llm/` | `OpenAICompatibleClient`, `ModelSettings` | 多 provider LLM 客户端（OpenAI/通义/豆包/DeepSeek） |
| `pandaren/tool/` | `Tool`, `ToolTier`, `ToolRegistry` | 工具注册、两层分级 |
| `pandaren/behavior/` | `PermissionGuard`, `HITLController`, `HarnessExecutor` | 权限守卫、人工审批、熔断 |
| `pandaren/engine/` | `AgentLoop`, `RunCoreMixin`, `MessageBuilder` | 8-Phase ReAct 核心 |
| `pandaren/memory/` | `Memory`, `ShortTermMemory`, `LongTermMemory` | 三层记忆、四层压缩管线 |
| `pandaren/hook/hooks.py` | `AgentHooks`, `DefaultAgentHooks` | 21 个生命周期扩展点 |
| `pandaren/observability/` | `AuditLog`, `Tracer`, `Metrics`, `Logger` | 四大观测支柱 |
| `pandaren/builder.py` | `AgentBuilder` | Fluent API 构建入口 |

---

## 📦 2. `pandapal/` — Agent 运行时后端

把 pandaren 引擎变成**完整可运行的 Agent 服务**的应用层：将引擎与桌面客户端、企业微信、智能音箱等渠道连接起来。

### 核心能力

- **IoC 容器**（`SubsystemContainer`）：`subsystem_registry.py` 声明 11+ 个子系统，自动拓扑排序 + 依赖注入
- **消息路由**（`MessageRouter`）：入站消息解析、去重、路由
- **Agent 调度**（`AgentScheduler` + `AgentExecutor`）：纯路由调度 + 流式执行引擎
- **HITL 管理**（`HITLManager` + `HITLBridge`）：审批暂停/恢复；`HITLBridge` 是审批状态的唯一 Owner
- **跨渠道广播**（`MessageBroadcast` + `ChannelRegistry`）：NormalizedEvent 分发到所有已连接渠道
- **桌面 IPC**（`StdioIpcServer` + `IpcStdoutTransport`）：与 Tauri 桌面端通过 stdin/stdout JSON Lines 通信
- **持久化**（`StorageManager`）：SQLite 存储，10+ Repository
- **任务调度**（`TaskScheduler`）：cron / event / manual 三种触发方式

### 消息流（端到端）

```
[桌面客户端] ──stdin JSON──→ StdioIpcServer ──→ InboundMessage
                                                          ↓
                                                   MessageRouter
                                                          ↓
                                              AgentScheduler / HITLBridge / TaskScheduler
                                                          ↓
                                                  AgentExecutor
                                               (agent.run_stream())
                                                          ↓
                                       StreamEvent → NormalizedEvent
                                                          ↓
                                               MessageBroadcast.send()
                                                          ↓
                          ┌───────────────┬───────────────┐
                          ↓               ↓               ↓
                 IpcStdoutTransport  WeComTransport  WSSGateway
                          ↓               ↓               ↓
                   [桌面客户端]      [企业微信]      [Relay Server]
```

### 快速开始

```bash
pip install -e ".[desktop]"       # 安装 pandapal 及其依赖
# `pandapal.local` 是桌面 sidecar 入口，通常由 Tauri 客户端自动拉起；
# 手动运行时需指定工作区与应用数据目录：
python -m pandapal.local --workdir <工作区目录> --app-data-dir <应用数据目录>
```

---

## 📦 3. `pandapal_desktop/` — 桌面客户端

基于 **Tauri v2 + React + TypeScript** 的桌面 GUI，本地化 Agent 交互体验——流式打字机回复、HITL 审批弹窗、Plan Mode 审批、任务进度面板等。

### 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | React 18 + TypeScript + Vite 5 |
| 桌面壳 | Tauri v2 (Rust) |
| 状态管理 | Zustand 4 |
| 路由 | React Router DOM 6 |
| 代码编辑器 | Monaco Editor (`@monaco-editor/react`) |
| IPC | `@tauri-apps/api`（invoke + event listen），**不使用 WebSocket** |

### 与后端的通信方式

```
用户 → React 前端
        ↓ invoke("send_message", ...)
       Rust (Tauri Core)
        ↓ write stdin
       Python Sidecar (PandaPal Agent)
        ↓ stdout "IPC:{json}"
       Rust → emit("backend-event", json)
        ↓ listen("backend-event")
       React 前端 (BackendProvider 分发)
```

Python sidecar 以 **PyInstaller onedir 模式**打包，由 Rust 通过 `std::process::Command` 启动。

### 开发

**前置条件**：Python ≥ 3.12、Node.js ≥ 18 + pnpm、Rust 工具链

```bash
cd pandapal_desktop
pnpm install

pnpm tauri:dev       # 构建 Python sidecar + 启动完整桌面应用（推荐）
pnpm dev             # 仅 Vite 前端（浏览器预览）
pnpm tauri build     # 打包安装包（Windows: .exe / macOS: .app/.dmg）
pnpm typecheck       # TypeScript 类型检查
```

---

## 📦 4. `pandapal_relay/` — 云端接入层（可选）

独立部署的 **FastAPI 服务**，通过 WebSocket 将外部渠道与本地 Agent 桥接。

> **可选性说明**：Relay 服务器与接入渠道均为**可选**——桌面端可完全本地运行，支持本地登录，无需任何云端组件。接入渠道按需启用：
>
> - **企业微信（WeCom）**：可选渠道，需要把 Agent 接入办公 IM 时启用；
> - **XiaoZhi 智能音箱**：尚在开发中，敬请期待。

### 核心能力

- **Agent WebSocket 接入**：`WS /relay/ws`——单个 Agent 连接，支持 50 条断连缓冲 + 重连 drain
- **企微接入**：`POST /assistant/wecom/callback`——XML 解析、AES 解密、去重、异步转发；同时作为 HITL 审批渠道（模板卡片审批）
- **XiaoZhi 智能音箱**：`WS /xiaozhi/ws`——Opus 音频帧缓冲、ASR 语音识别、TTS 语音合成
- **账号系统**：REST 注册 / 登录 / 修改密码，bcrypt 哈希、JWT 签名、SQLite 持久化
- **多渠道回复路由**：链式分发（XiaoZhi → 企微 → 兜底）

### API 端点

| 端点 | 说明 |
|------|------|
| `POST /auth/register` | 用户注册 |
| `POST /auth/login` | 用户登录 |
| `PUT /auth/password` | 修改密码 |
| `POST /assistant/wecom/callback` | 企微消息接收 |
| `WS /relay/ws` | Agent 连接 |
| `WS /xiaozhi/ws` | XiaoZhi 设备连接 |
| `GET /health` | 健康检查 |

### 快速开始

```bash
cd pandapal_relay
pip install -e ..                 # 从仓库根 editable 安装（含全部依赖）
# 配置 .env（参考 pandapal_relay/.env.example）
python -m pandapal_relay         # 监听 0.0.0.0:8090
curl http://localhost:8090/health
```

---

## 📖 文档

- [PANDAPAL.md](PANDAPAL.md) — 项目全景与技术架构（四个子项目、模块速查、设计原则）
- [README.md](README.md) — English README

## 🧪 测试

```bash
pip install -e ".[dev]"
pytest                       # 收集 pandapal/、pandaren/、scripts/ 下所有测试
```

代码规范使用 [ruff](https://github.com/astral-sh/ruff)（`pyproject.toml` 已配置）。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 🔒 安全

发现安全漏洞？请通过 [SECURITY.md](SECURITY.md) 描述的报告渠道告知我们，请勿公开披露。

## 📄 许可证

[MIT](LICENSE) © 2026 keanezhang
