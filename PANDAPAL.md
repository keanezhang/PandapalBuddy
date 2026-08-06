# PANDAPAL.md — PandaPal Buddy 项目指引

> 本文件为 LLM 系统提示词中的项目上下文锚点。每次推理前请优先阅读，避免重复探索项目结构。

---

## 项目全景

**PandaPal Buddy** 是一个企业级 Python Agent 构建框架，核心理念：**让 Agent 的每一步都「看得见、管得住」**。

仓库包含 **4 个子项目**，分层协作：

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

> 根目录另有 `monaco-inline-diff-review/`（Monaco 内联 diff 审查组件，独立子工程）与 `docs/`（设计文档，本地保留）。`CLAUDE.md` 是本文件的同步副本。

---

## 一、pandaren — Agent SDK 核心库

**版本**：0.1.0 | **Python**：≥ 3.12 | **入口**：`pandaren/builder.py`

### 1.1 四层分层架构

```
Layer 4: engine/         AgentLoop (8-Phase ReAct)、消息构建、输出解析、流式事件
Layer 3: behavior/       PermissionGuard、HITLController、Harness（运行时保护）
Layer 2: capability/     llm/ + tool/ + skill/ + memory/
Layer 1: identity/       Identity、Permission、TrustLevel（不可变地基）

横切: observability/     AuditLog、Tracer、Metrics、Logger（四大观测支柱）
横切: hook/              AgentHooks（21 个统一生命周期扩展点）
横切: plan/              PlanManager（Plan Mode 规划-审批-执行）
```

**依赖方向严格单向**：`engine → behavior → capability → identity`

### 1.2 模块速查表

| 模块 | 路径 | 核心类 | 职责 |
|------|------|--------|------|
| identity | `pandaren/identity/` | `Identity`, `Permission`, `TrustLevel` | 不可变身份声明、权限集合 |
| llm | `pandaren/llm/` | `OpenAICompatibleClient`(client.py), `ResponsesAPIClient`(responses_client.py), `ModelSettings`, `LLMRouter` | 多 provider LLM 客户端（含 `capabilities.py`、`router.py`、`cache_strategy.py`、`cache_usage.py`） |
| tool | `pandaren/tool/` | `Tool`, `ToolTier`, `ToolRegistry` | 工具框架，5 个子包：`builtin/` `definition/` `exposure/` `execution/` `registry/` |
| tools | `pandaren/tools/` | ask_user / bash / glob / grep / math_calculator / time / `file_tool/` | 内置工具实现（读写编辑删除列目录） |
| skill | `pandaren/skill/` | `Skill`, `SkillRegistry` | 知识注入、自动触发控制（含 `script_loader.py`） |
| memory | `pandaren/memory/` | `Memory`, `ShortTermMemory`, `LongTermMemory`（另含 `working_memory.py`） | 三层记忆 + 工作记忆、压缩管线；子目录 `backends/` `compaction/` `reinject/` |
| behavior | `pandaren/behavior/` | `PermissionGuard`, `HITLController`, `HarnessExecutor` | 权限守卫、人工审批、熔断（含 `harness/` 子目录；另有 `context_window_budget`/`execution_limits`/`step_guard`） |
| engine | `pandaren/engine/` | `AgentLoop`(loop.py), `RunCoreMixin`(run_core.py), `MessageBuilder` | 8-Phase ReAct 核心 |
| stream | `pandaren/engine/stream.py` | `StreamEventType`, `StreamEvent` | 17 种流式事件 |
| agent | `pandaren/agent/` | `Agent`, `AgentStatus`, `AgentBlueprint` | 顶层 Agent |
| sub_agent | `pandaren/sub_agent/` | `SubAgentRegistry`, `SubAgentBlueprint` | 多 Agent 委派（内置提示词见 `pandaren/agents/`） |
| plan | `pandaren/plan/` | `PlanManager` | Plan Mode：规划文件、审批、执行 |
| hook | `pandaren/hook/hooks.py` | `AgentHooks`, `DefaultAgentHooks`, `CompositeAgentHooks` | 21 个生命周期扩展点 |
| observability | `pandaren/observability/` | `AuditLog`, `Tracer`, `Metrics`, `Logger` | 四大观测支柱；`backend/` 含 console/in_memory/markdown/sqlite 四套后端 |
| cancellation | `pandaren/cancellation.py` | `CancelToken`, `CancelledSignal` | 外部取消信号 |
| constants | `pandaren/constants.py` | `CHARS_PER_TOKEN`, `DEFAULT_CONTEXT_WINDOW` 等 | 全局魔法数字收编 |
| builder | `pandaren/builder.py` | `AgentBuilder` | Fluent API 构建入口 |
| utils | `pandaren/utils/` | `path_utils` / `file_validators` / `project_root` | 路径处理、文件校验、项目根定位 |

> SDK 自带测试在 `pandaren/test/`（`mock/` + `real/` 各 14 个文件）；引擎单测在 `pandaren/engine/tests/`。pytest `testpaths = ["pandapal", "pandaren", "scripts"]`。

### 1.3 关键设计原则（最重要 5 条）

1. **HC1**：`Identity` 用 `__slots__` + `__setattr__` 物理阻断修改，运行时不得升权
2. **HC4**：`AuditLog` 不可关闭，任何代码路径都不得绕过审计写入
3. **O3**：`Agent.run()` 永不向外抛异常，所有异常必须在内部转换为 `AgentResult`
4. **S3**：不得继承 `Identity`，防止通过子类伪造身份
5. **R3**：熔断阈值触发后必须立即终止循环

### 1.4 AgentBuilder Fluent API（快速参考）

```python
agent = (
    AgentBuilder()
    .identity(agent_id="my_agent", agent_name="助手", ...)
    .llm(client=OpenAICompatibleClient.for_openai(...))
    .llm_settings(temperature=0.7, max_tokens=4096, ...)
    .tools([search_web, calc])
    .skills([domain_knowledge])          # 或 .skills_from_dir(...)
    .sub_agents([...])                   # 或 .sub_agents_from_dir(...) / .with_default_sub_agents()
    .plan_mode(enabled=True, ...)        # Plan Mode
    .system_prompt("你是一个助手...")
    .hooks(MyAgentHooks())
    .behavior(max_steps=30, step_timeout=30.0, total_timeout=300.0, ...)
    .context_budget(context_window=128000, ...)
    .memory(persist=True, session_mode="multi_turn", ...)
    .observability(audit=None, tracer=None, ...)
    .build()                             # 或 .build_blueprint() → AgentBlueprint
)
result = await agent.run("用户输入", session_id="session-001")
```

共 **16 个 fluent 方法** + 2 个构建出口（`.build()` / `.build_blueprint()`）。

### 1.5 重要枚举

| 枚举 | 值 | 说明 |
|------|----|------|
| `TrustLevel` | EXTERNAL=1, SUB_AGENT=2, ORCHESTRATOR=3 | 信任分级（IntEnum，支持大小比较） |
| `SensitivityLevel` | LOW=1, MEDIUM=2, HIGH=3, CRITICAL=4 | 操作敏感度（IntEnum） |
| `ToolTier` | ALWAYS=1(≤15个), DEFERRED=2 | 工具暴露等级 |
| `CircuitState` | CLOSED=1, OPEN=2, HALF_OPEN=3 | 熔断器状态 |
| `AuditEventType` | 21种(见 observability/types.py) | 审计事件（含 Agent Registry 域） |
| `SpanType` | 8种(RUN/STEP/LLM_CALL/TOOL_CALL/GUARD_CHECK/HITL_CHECK/MESSAGE_BUILD/SKILL_INVOKE) | 追踪 Span |
| `StreamEventType` | 17种(见 engine/stream.py) | 流式事件（含 REASONING_TOKEN/CANCELLED/PLAN_APPROVAL_REQUESTED） |

### 1.6 LLM Provider 命名约定

代码中用**平台名**而非品牌名：`openai`(GPT)、`dashscope`(通义千问)、`volcengine`(豆包)、`deepseek`(DeepSeek)。
`openai` 即 `OpenAICompatibleClient`（`client.py`）的基础兼容协议；其余平台为插件：`pandaren/llm/providers/` 当前含 `dashscope.py` + `volcengine.py`（新增见 `ADDING_A_PROVIDER.md` 与 `_template.py.example`）。

### 1.7 11 个可替换扩展点

| # | 扩展点 | Protocol | 使用场景 |
|---|--------|----------|---------|
| 1 | `.memory(compression_policy=...)` | `CompressionPolicy` | 自定义上下文压缩 |
| 2 | `.memory(session_summary_policy=...)` | `SessionSummaryPolicy` | 会话摘要(需LLM→应用层) |
| 3 | `.memory(raw_log_backend=...)` | `RawLogBackend` | 替换对话日志存储 |
| 4 | `.memory(summary_backend=...)` | `SummaryBackend` | 替换摘要存储 |
| 5 | `.observability(audit=...)` | `AuditBackend` | 审计后端(HC4不可关闭) |
| 6 | `.observability(tracer=...)` | `TracerBackend` | OpenTelemetry接入 |
| 7 | `.observability(metrics=...)` | `MetricsBackend` | Prometheus接入 |
| 8 | `.observability(log=...)` | `LoggerBackend` | 自定义日志平台 |
| 9 | `.observability(sanitizer=...)` | `Sanitizer` | 敏感数据脱敏 |
| 10 | `.hooks(...)` | `AgentHooks` | 21个生命周期hook |
| 11 | `.context_budget(token_estimator=...)` | `TokenEstimator` | Token精确计数 |

### 1.8 8-Phase ReAct 循环

```
Phase 1: 上下文预测 → Phase 2: 消息构建 → Phase 3: LLM 调用 → Phase 4: 输出解析
→ Phase 5: 工具选择 → Phase 6: 工具执行 → Phase 7: 结果收集 → Phase 8: 决策
```

执行内核统一在 `RunCoreMixin._run_stream_core()`（`engine/run_core.py`）中：
- `run()` → drain 内核，永远返回 `AgentResult`（O3）
- `run_stream()` → passthrough，逐个 yield `StreamEvent`

---

## 二、pandapal — Agent 运行时后端

**定位**：pandaren SDK 的**应用层**，将 Agent 引擎与各种输入/输出渠道连接起来，提供完整可运行的 Agent 服务。

**启动方式**：`python -m pandapal.local`（`local/__main__.py` → `run_local.py` 装配全栈 → `PandaPalApp.start()`）。

### 2.1 顶层结构

```
pandapal/
├── app.py                     # ★ 唯一启动入口：PandaPalApp + run_pandapal()
├── subsystem_container.py     # IoC 容器：SubsystemContainer/AppContext，自动拓扑排序 + 依赖注入
├── subsystem_registry.py      # 集中注册表：16 个子系统的 factory + 依赖声明
├── session_id.py              # ★ session_id 单一真相源（顶层零依赖，见 §八）
├── degradation.py             # ★ 统一降级可观测通道（log + Metrics，见 §九）
├── dispatch/                  # 入站分发层（2026 新增）
│   ├── dispatcher.py          #   InboundDispatcher（Gateway/IPC 共享）
│   ├── pipeline.py            #   InboundPipeline 接线
│   └── adapter.py / types.py
├── scheduler/                 # Agent 调度层
│   ├── scheduler.py           #   AgentScheduler (纯路由)
│   ├── executor.py            #   AgentExecutor (流式执行引擎)
│   ├── agent_pool.py          #   SessionAgentPool (多会话并发池 + evict)
│   ├── hitl_manager.py        #   HITLManager (HITL 暂停/恢复)
│   ├── interaction_manager.py #   InteractionManager (ask_user 问卷)
│   ├── plan_manager.py        #   PlanModeManager (Plan Mode 审批)
│   ├── reply_manager.py       #   ReplyIdManager (回复归属)
│   ├── background.py          #   spawn_background (故障隔离点)
│   └── stream_to_normalized.py#   StreamEvent → NormalizedEvent 转换
├── events/
│   └── normalized.py          #   NormalizedEvent (frozen dataclass, 46种 EventType)
├── router/                    # 消息路由层
│   ├── router.py              #   MessageRouter (解析 + 去重 + 路由)
│   └── models.py              #   InboundMessage / GatewayProtocol
├── messages/
│   └── types.py               #   RouterMessageType (9种路由词汇) / HITLDecision
├── broadcast/                 # 跨渠道统一广播层
│   ├── broadcaster.py         #   MessageBroadcast
│   ├── channel_registry.py    #   ChannelRegistry
│   ├── transport.py           #   Transport Protocol（渠道抽象源，relay 侧有副本）
│   ├── channel_ids.py / models.py
│   └── policy.py              #   DispatchPolicy / EventCategory (每渠道 env 可配)
├── desktop_ipc/               # 桌面 IPC 协议层 (stdio JSON Lines)
│   ├── stdio_ipc.py           #   StdioIpcServer (stdin 解析)
│   ├── ipc_transport.py       #   IpcStdoutTransport (出站 + 会话级漏 stamp 护栏)
│   ├── inbound_adapter.py     #   入站 IPC 适配（→ InboundDispatcher）
│   └── message_codec.py       #   IpcMessageType 编解码（前后端契约真相源）
├── gateway/                   # Gateway 通信层 (WebSocket → Relay)
│   ├── gateway.py             #   Gateway (WSS plumbing, 集中式转发)
│   ├── wss_transport.py       #   WSSGateway (Transport 适配器)
│   └── inbound_adapter.py / models.py / types.py
├── hitl/                      # HITL 审批桥接
│   ├── bridge.py              #   HITLBridge (唯一审批状态 Owner)
│   └── approval_log.py
├── session/                   # 会话管理
│   ├── manager.py             #   SessionManager
│   ├── session_list_manager.py#   SessionListManager (会话列表/分组/历史)
│   └── session_list_handler.py
├── storage/                   # 数据持久化层
│   ├── manager.py             #   StorageManager
│   └── repositories/          #   sqlite_* + markdown_* 双实现（session/task/approval/
│                              #   run_state/device/agent_task/session_group/avatar_config
│                              #   + raw_log/summary/working_memory backend）
├── task_scheduler/            # 定时任务调度 (cron/event/manual)
├── budget/                    # 预算额度（按 provider 分账，handler.py）
├── dashboard/                 # 看板聚合（aggregator + sqlite_aggregator + handler）
├── quality/                   # 编码质量门控（checker/gate，渲染零漂移回归）
├── hooks/                     # 应用层 hook 装配（skill_hooks.py）
├── resources/                 # 内置资源（agents/ + skills/ + tokenizer/ + skill_manager.py）
├── tools/                     # Agent 工具集
│   ├── agent_task_tools.py    #   Agent 任务管理工具
│   ├── scheduler_tools.py     #   调度管理工具
│   ├── app_data_tools.py      #   快应用数据推送工具
│   ├── progress_tools.py      #   进度上报工具
│   └── web_tools.py           #   Web 搜索/抓取
├── config/                    # 配置管理
└── local/                     # 本地 Agent 启动入口
    ├── run_local.py           #   装配全栈 → PandaPalApp.start()
    ├── llm_policies.py        #   LLM 策略配置
    ├── prompts.py             #   系统提示词装配
    ├── boot_logger.py         #   启动日志
    └── TEST_RULE.md           #   编码收尾测试闭环规则
```

### 2.2 核心架构：IoC 容器 + 声明式注册

**PandaPalApp** 是唯一启动入口，内部持有 `SubsystemContainer`（IoC 容器）：

- **自动拓扑排序**：子系统在 `subsystem_registry.py` 中声明 `needs` 依赖关系，容器自动决定启动顺序
- **依赖注入**：每个子系统通过 factory 函数声明所需依赖（类型依赖 + `context_needs`）
- **失败隔离**：单个子系统启动失败不阻塞其他

**16 个注册子系统**（`register_pandapal_subsystems()`）：

| # | 名称 | 产物 | start |
|---|------|------|-------|
| 1 | `registry` | ChannelRegistry | – |
| 2 | `ipc_transport` | IpcStdoutTransport | – |
| 3 | `broadcast` | MessageBroadcast | ✓ (注册 5 渠道) |
| 4 | `router` | MessageRouter | – |
| 5 | `hitl` | HITLBridge | – |
| 6 | `interaction_manager` | InteractionManager | – |
| 7 | `hitl_manager` | HITLManager | – |
| 8 | `plan_manager` | PlanModeManager | – |
| 9 | `session_pool` | SessionAgentPool | ✓ (evict 循环) |
| 10 | `session_list_manager` | SessionListManager | – |
| 11 | `agent_scheduler` | AgentScheduler | – |
| 12 | `task_scheduler` | TaskScheduler | ✓ |
| 13 | `scheduler_tools` | SchedulerTools | – |
| 14 | `agent_task_tools` | AgentTaskTools | – |
| 15 | `app_data_tools` | AppDataTools | – |
| 16 | `progress_tools` | ProgressTools | – |

渠道分发策略：每渠道独立 env 键 `PANDAPAL_CHANNEL_{DESKTOP_IPC|WECOM|XIAOZHI}_POLICY`（`shared|source_only|target_only`），默认 desktop_ipc/wecom=SOURCE_ONLY，xiaozhi=TARGET_ONLY。

### 2.3 消息流（完整链路）

```
[桌面客户端] ──stdin JSON──→ StdioIpcServer ──→ InboundDispatcher ──→ InboundMessage
[Gateway/WSS] ──────────────┘                            ↓
                                                  MessageRouter
                                               (解析/去重/路由/超时)
                                                         ↓
                    ┌─────────────────┬──────────────────┼──────────────┐
                    ↓                 ↓                  ↓              ↓
            AgentScheduler      HITLBridge       PlanModeManager  TaskScheduler
         (USER_INSTRUCTION)  (APPROVAL_*)      (PLAN_APPROVAL_DECISION)
                    ↓
            SessionAgentPool (并发闸门)
                    ↓
            AgentExecutor
         (调用 agent.run_stream())
                    ↓
         StreamEvent → NormalizedEvent
                    ↓
           MessageBroadcast.send()
                    ↓
    ┌───────────────┼───────────────┐
    ↓               ↓               ↓
IpcStdoutTransport  WeComTransport  WSSGateway
    ↓               ↓               ↓
[桌面客户端]    [企业微信]      [Relay Server]
```

**RouterMessageType（9 种路由词汇）**：`USER_INSTRUCTION`, `APPROVAL_DECISION`, `TASK_INSTRUCTION`, `TASK_RESULT`, `APPROVAL_NEEDED`, `APPROVAL_RESPONSE`, `INTERACTION_RESPONSE`, `PLAN_APPROVAL_DECISION`, `STOP_GENERATION`（直通 IPC 词汇的权威来源是 `desktop_ipc/message_codec.py` 的 `IpcMessageType`）。

### 2.4 关键 API

| API | 说明 |
|-----|------|
| `PandaPalApp(config, blueprint, session_manager, storage_manager, ...)` | 应用容器（blueprint 为 None 即 fail-fast） |
| `await app.start()` | 启动所有子系统（幂等） |
| `MessageRouter` | 消息路由核心（解析/去重/路由） |
| `AgentScheduler` | 纯路由调度器（不含暂停逻辑） |
| `AgentExecutor` | 流式执行引擎 |
| `SessionAgentPool` | 多会话并发池（queued/started/released 三态反馈） |
| `MessageBroadcast` | 出站广播器 |
| `HITLBridge` | 审批桥接（唯一状态Owner） |
| `NormalizedEvent` | 跨渠道统一事件（46种EventType） |
| `StorageManager` | 统一持久化入口（SQLite + Markdown 双后端） |
| `session_id_mod` (`pandapal/session_id.py`) | session_id 创建/校验/断言唯一入口（`require`/`assert_consistent`） |
| `pandapal.degradation` | 降级统一通道（`event_code` 主键 + Metrics counter） |

**EventType 分组（46 种）**：流式生命周期 4（REPLY_START/END、RUN_START/END）、LLM 输出 2、工具 2、暂停/恢复 4（HITL_REQUEST/INTERACTION_REQUEST/PERMISSION_DENIED/AGENT_HALTED）、终端 2（ERROR/APPROVAL_RESULT）、系统 4（USER_INPUT_ECHO/TASK_NOTIFICATION/AGENT_TASK_EVENT/AGENT_REPLY）、Plan Mode 1、Quick App 1（QUICK_APP_DATA，前端静默消费）、技能 9（PROGRESS/LIST/GET/SAVED/DELETED/IMPORTED/EXPORTED/ACTIVATED/CLEARED）、定时任务 2、并发池 1、会话列表 6、搜索 1、模型 1、凭据 4、看板 1、预算 1。出站会话级事件必带 `payload["session_id"]`；全局级事件显式 `scope=global`（`EVENT_SCOPE_KEY`）。

### 2.5 对 pandaren 的依赖

pandapal **依赖 pandaren**，主要体现在 scheduler/executor.py 调用 `agent.run_stream()`，以及 tools 中的工具实现基于 pandaren 的 Tool 基类。pandapal 是 pandaren 的消费者。

---

## 三、pandapal_desktop — Tauri 桌面客户端

**定位**：基于 Tauri v2 + React + TypeScript 构建的桌面 GUI 应用，提供与 PandaPal AI Agent 的图形化交互体验。

### 3.1 技术栈

| 层级 | 技术 |
|------|------|
| 前端框架 | React 18 + TypeScript + Vite 5 |
| 桌面壳 | Tauri v2 (Rust) |
| 状态管理 | Zustand 4 |
| 路由 | React Router DOM 6 |
| 代码编辑器 | Monaco Editor (`@monaco-editor/react`，本地加载不走 CDN) |
| IPC 通信 | `@tauri-apps/api` (invoke + event listen)，**不使用 WebSocket** |
| 数据持久化 | tauri-plugin-store / tauri-plugin-fs |

### 3.2 通信架构

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

- **不使用 WebSocket**：前端通过 Tauri invoke 调用 Rust command
- Python 进程以 **onedir 模式**（PyInstaller 打包）由 Rust 通过 `std::process::Command` 启动
- 前端通过 `listen("backend-event")` 接收 Python 的异步推送

### 3.3 目录结构

```
pandapal_desktop/
├── src/                          # React 前端
│   ├── App.tsx                   # 路由根组件（AuthGuard + ChatLayout）
│   ├── main.tsx                  # 入口（BrowserRouter + BackendProvider）
│   ├── monaco-setup.ts           # Monaco 本地加载配置
│   ├── pages/
│   │   ├── ChatPage.tsx          # ★ 核心聊天页（/）
│   │   ├── LoginPage.tsx / RegisterPage.tsx
│   │   ├── ModelConfigWizard.tsx # 模型配置向导（/model-config）
│   │   ├── SkillsPage.tsx        # 技能列表/详情
│   │   ├── SkillEditorPage.tsx   # 独立技能编辑器
│   │   ├── TasksPage.tsx         # 任务安排（/tasks）
│   │   ├── DashboardPage.tsx     # 看板（/dashboard，前端最大文件）
│   │   ├── SessionGroupPage.tsx  # 分组会话列表（/groups/:groupId）
│   │   └── dashboard/            #   看板 constants + derive
│   ├── components/
│   │   ├── ChatArea/             # MessageList/MessageBubble/StreamingBubble/Timeline
│   │   │   └── toolRenderers/    #   按工具渲染（AskUser/Bash/Edit/Read/Write/WebFetch...）
│   │   ├── fileRenderers/        # Html/Image/Markdown/Pdf/Table/Log
│   │   ├── pet/                  # 桌宠（FloatingPet/PetSprite/PetStore）
│   │   ├── TaskPanel/            # 任务面板
│   │   ├── AuthGuard.tsx / ChatLayout.tsx    # 路由守卫 / 聊天布局
│   │   ├── LeftSidebar.tsx       # 会话/分组/技能/文件树
│   │   ├── InputBar.tsx          # 消息输入栏
│   │   ├── HitlModal.tsx / PlanApprovalModal.tsx  # HITL / Plan Mode 审批弹窗
│   │   ├── InteractionInline.tsx # ask_user 问卷内联渲染
│   │   ├── CommandPalette.tsx    # ⌘K 全局搜索
│   │   ├── CredentialForm.tsx / CredentialGate.tsx / ModelServiceSettings.tsx  # BYOK 凭据
│   │   ├── SessionListPanel.tsx / SettingsPanel.tsx / TopToolbar.tsx
│   │   ├── BudgetBar.tsx / BudgetSettingsModal.tsx  # 预算额度条 / 额度设置
│   │   ├── TaskNotificationModal.tsx / SplitDivider.tsx
│   │   └── FileExplorer.tsx / FileViewerPanel.tsx / Wallpaper*.tsx / WorkspaceGate.tsx
│   ├── hooks/
│   │   └── usePetReactions.ts    # Agent 活动 → 宠物动画映射
│   ├── providers/
│   │   └── BackendProvider.tsx   # IPC 连接管理 + 消息分发中心
│   ├── store/                    # 22 个 Zustand stores（见 3.4）
│   ├── styles/
│   │   └── global-v2.css
│   └── types/
│       ├── api.ts                # ★ IPC 消息类型定义（与后端 codec 对齐）
│       ├── dashboard.ts / pet.ts
├── src-tauri/                    # Tauri Rust 后端
│   ├── bin/                      # 编译好的 Python sidecar 二进制
│   │   └── pandapal-sidecar-x86_64-pc-windows-msvc
│   ├── src/
│   │   ├── lib.rs                # Tauri Builder + Commands 注册 + 凭据/会话命令
│   │   ├── main.rs / sidecar.rs  # sidecar 进程生命周期
│   │   ├── auth.rs               # 登录态命令（token 存取）
│   │   ├── workspace.rs          # 工作区命令
│   │   ├── pets.rs               # 桌宠目录/安装命令
│   │   └── lifecycle.rs / tray.rs
│   └── capabilities/             # Tauri 权限声明
└── vite.config.ts / package.json
```

> **注意**：Quick App 框架（`src/pipeline/` `src/framework/` `src/apps/`）已移除。`QUICK_APP_DATA` 消息类型仍保留在协议中（兼容 wire format），但前端静默消费。

### 3.4 Zustand Stores（22 个）

对话核心 `chatStore`（流式 token/时间线/工具调用）；连接与认证 `connectionStore` `authStore`；会话 `sessionStore` `groupViewStore` `sessionConcurrencyStore`；模型与凭据 `modelStore` `credentialStore`（BYOK，主键=(provider, model_id)）；审批 `hitlStore` `planApprovalStore`；任务与看板 `agentTaskStore` `taskSchedulerStore` `dashboardStore`；额度 `budgetStore`；技能 `skillStore`；文件 `fileStore`；工作区 `workspaceStore`；UI `preferenceStore`(persist) `commandPaletteStore` `searchStore` `wallpaperStore` `petStore` 等。

### 3.5 Rust Commands（前端 invoke 调用）

| 域 | Command | 用途 |
|----|---------|------|
| 消息 | `send_message` | 发送用户消息（必带消息所属 sessionId） |
| 消息 | `send_session_ipc` / `set_current_session_id` | 会话级 IPC 透传 / 设置当前会话 |
| 审批 | `send_hitl_decision` / `send_interaction_response` / `send_plan_approval_decision` | 三类暂停的恢复决策 |
| 控制 | `stop_generation` / `send_ping` / `quit_app` | 停止生成 / 心跳 / 退出 |
| sidecar | `start_sidecar` / `get_auth_token` | 启动后端 / 取 token |
| 凭据 | `check_llm_credentials` / `save_llm_credentials` / `get_provider_catalog` / `get_model_prices` | BYOK 凭据与模型目录 |
| 技能 | `request_skill_list` / `request_skill_detail` / `save_skill` / `delete_skill` / `import_skill` / `export_skill` | 技能 CRUD |
| 任务/看板 | `request_scheduled_tasks` / `delete_scheduled_task` / `request_dashboard` / `search_request` | 拉取式数据请求 |
| 认证 | `auth_notify_ready` / `auth_verify_token` / `auth_get_token` / `auth_get_username` / `auth_update_token` / `auth_logout` | 登录态管理 |
| 工作区 | `open_workspace` / `get_recent_workspaces` / `get_current_workspace` | 工作区管理 |
| 桌宠 | `install_pet_urls` / `fetch_pet_catalog` / `list_pets` / `remove_pet` | 桌宠资源管理 |

> 旧 `switch_situation` 已移除，会话切换走 `SESSION_SWITCH` IPC（`send_session_ipc`）。

### 3.6 IPC 消息类型（真相源：`pandapal/desktop_ipc/message_codec.py` ⇄ `src/types/api.ts`）

**入站**（前端 → Python，约 30 种）：
- 核心：`SEND_MESSAGE`, `HITL_DECISION`, `INTERACTION_RESPONSE`, `PLAN_APPROVAL_DECISION`, `STOP_GENERATION`, `PING`
- 会话管理：`SESSION_LIST_REQUEST`, `SESSION_CREATE`, `SESSION_SWITCH`, `SESSION_DELETE`, `SESSION_FAVORITE_TOGGLE`, `SESSION_GROUP_MUTATE`, `SESSION_HISTORY_REQUEST`
- 技能：`SKILL_LIST`, `SKILL_GET`, `SKILL_SAVE`, `SKILL_DELETE`, `SKILL_IMPORT`, `SKILL_EXPORT`
- 凭据：`LOAD_CREDENTIALS`, `SAVE_LLM_CREDENTIALS`, `VERIFY_CREDENTIALS`, `GET_CREDENTIALS_STATUS`
- 其他：`MODEL_LIST_REQUEST`, `REQUEST_SCHEDULED_TASKS`, `DELETE_SCHEDULED_TASK`, `SEARCH`, `DASHBOARD_REQUEST`, `SET_BUDGET`, `BUDGET_QUERY`

**出站**（Python → 前端，约 48 种）：
- 流式：`REPLY_START`, `TOKEN`, `REASONING_TOKEN`, `REPLY_END`, `TOOL_START`, `TOOL_END`
- 暂停/恢复：`HITL_REQUEST`, `INTERACTION_REQUEST`, `PLAN_APPROVAL_REQUEST`, `PERMISSION_DENIED`, `AGENT_HALTED`
- 终端/系统：`APPROVAL_RESULT`, `ERROR`, `PONG`, `USER_INPUT_ECHO`, `AGENT_REPLY`, `TASK_NOTIFICATION`, `AGENT_TASK_EVENT`, `QUICK_APP_DATA`(静默消费), `SKILL_PROGRESS`
- 数据响应：`SESSION_LIST`, `SESSION_SWITCHED`, `SESSION_UPDATED`, `SESSION_DELETED`, `SESSION_GROUP_LIST`, `SESSION_HISTORY_LIST`, `SESSION_CONCURRENCY`, `SEARCH_RESULT`, `MODEL_LIST`, `CREDENTIALS_LIST`, `CREDENTIALS_SAVED`, `CREDENTIALS_VERIFIED`, `CREDENTIALS_STATUS`, `DASHBOARD_DATA`, `BUDGET_STATUS`, `SCHEDULED_TASK_LIST`, `SCHEDULED_TASK_CHANGED`, `SKILL_LIST_RESULT`, `SKILL_GET_RESULT`, `SKILL_SAVED`, `SKILL_DELETED`, `SKILL_IMPORTED`, `SKILL_EXPORTED`, `SKILL_ACTIVATED`, `SKILL_CLEARED`
- 认证（JWT 自动续期）：`AUTH_TOKEN_REFRESHED`（带新 token，前端回写 store）、`AUTH_EXPIRED`（登录态失效，前端登出）
- 兜底：`UNKNOWN`（未知事件类型兜底）

### 3.7 对 pandapal 的依赖

桌面前端通过 stdin/stdout IPC 与 pandapal 通信。`src/types/api.ts` 中的消息类型与 `pandapal/desktop_ipc/message_codec.py` 保持严格一致（互为"真相源"注释标注）。

---

## 四、pandapal_relay — 云端 Relay Server

**定位**：独立部署的 FastAPI 应用，将企微、XiaoZhi 智能音箱等外部渠道的消息转发给本地 Agent。

### 4.1 架构定位

```
┌─────────────────────┐   WebSocket (JWT 鉴权)  ┌─────────────────────────────┐
│  PandaPal Backend   │ ◄══════════════════════► │  PandaPal Relay (FastAPI)   │
│  (本地 sidecar)     │     /relay/ws            │  端口 8090 (RELAY_PORT)     │
└─────────────────────┘                          └─────────────────────────────┘
                                                          │
                                   ┌──────────────────────┼──────────────────────┐
                                   │                      │                      │
                            GET+POST /assistant/    WS /xiaozhi/ws          GET /health
                            /wecom/callback              │                  GET /relay/channels
                                   │                      │
                            ┌──────────┐          ┌──────────────┐
                            │  企业微信  │          │  XiaoZhi     │
                            │  服务器    │          │  智能音箱     │
                            └──────────┘          └──────────────┘
```

### 4.2 目录结构

```
pandapal_relay/
├── run_relay.py               # 启动入口，组装 FastAPI app（fail-fast 配置校验）
├── __main__.py                # `python -m pandapal_relay` 的模块入口
├── server.py                  # WebSocket 端点 /relay/ws + /relay/channels + 消息转发
├── config.py                  # 环境变量配置（WeCom/XiaoZhi/Auth/Server）
├── message_types.py           # RouterMessageType/HITLDecision（本地副本，与 pandapal 同步）
├── normalized_events.py       # NormalizedEvent 模型（本地副本）
├── router_models.py           # InboundMessage 模型（本地副本）
├── transport_protocol.py      # Transport Protocol（本地副本）
├── wecom_bridge.py            # 企微桥接器：GET URL 验证 + POST 回调解密、消息转发
├── wecom_transport.py         # WeComRestTransport：消息推送到企微
├── xiaozhi_bridge.py          # XiaoZhi 桥接器：WS连接、音频缓冲、ASR/TTS
├── wecom/                     # 企微底层支持
│   ├── crypto.py              #   消息加解密(AES+SHA1)
│   └── sender.py              #   主动发送(token管理)
├── xiaozhi/                   # XiaoZhi 底层支持
│   ├── asr.py                 #   语音识别(Mock/DashScope/QwenRealtime/Whisper)
│   ├── tts.py                 #   语音合成(Mock/EdgeTTS/DashScope/QwenRealtime)
│   └── models.py              #   设备会话模型、协议版本
├── auth/                      # 账号认证系统
│   ├── service.py             #   注册/登录/JWT/密码修改/refresh（bcrypt + aiosqlite）
│   ├── router.py              #   Auth HTTP 路由（/auth/*）
│   └── models.py              #   Auth 数据模型
├── tests/                     # 单元测试
├── .env.example               # 环境变量模板
└── .env.development           # 开发环境配置（.env 缺失时 fallback）
```

### 4.3 核心功能

1. **Agent WebSocket 接入** — `WS /relay/ws?token=<jwt>` 强制 JWT 验签（无效 `close(4003)`）；单 Agent（新连接踢旧连接）；断连缓冲 50 条 + 重连 drain；ACK 可靠投递（`_pending_ack` TTL 300s）；心跳 90s 无活动 `close(4001)`
2. **企微接入** — `GET /assistant/wecom/callback` URL 验证 + `POST` 接收消息，XML解析、AES解密、去重、异步转发。支持审批按钮回调
3. **XiaoZhi 智能音箱** — `WS /xiaozhi/ws` 接收设备连接（hello 握手 + 协议版本校验），Opus 音频帧缓冲、ASR 语音识别、TTS 语音合成回复
4. **HITL 审批桥接** — 企微作为 HITL 渠道，模板卡片发送审批请求，用户点击后转发回 Agent
5. **账号系统** — REST 接口：注册（注册即登录）、登录（防枚举统一 401 + 锁定 423）、密码修改（JWT + 旧密码）、`refresh` 宽限期换发（60s 最小间隔）。bcrypt 哈希、JWT 签名、SQLite 持久化
6. **多渠道回复路由** — `register_reply_handler` 链式尝试分发（XiaoZhi → WeCom → 兜底 `register_agent_reply_handler`）
7. **Fail-Fast 配置** — `AUTH_JWT_SECRET` 永远必填；WeCom 启用时 agent_id/app_secret/token/aes_key 全部必填，且 `WECOM_DEFAULT_USER_ID` 必填、白名单非空、default user 必须在白名单内（三项不满足拒绝启动）

### 4.4 启动与端点

```bash
python -m pandapal_relay
# 监听 0.0.0.0:8090（RELAY_PORT 可配）
```

| 端点 | 说明 |
|------|------|
| `POST /auth/register` | 用户注册（注册即登录，返回 JWT） |
| `POST /auth/login` | 用户登录 |
| `PUT /auth/password` | 修改密码（Bearer JWT） |
| `POST /auth/refresh` | 宽限期内换发新 JWT |
| `GET /assistant/wecom/callback` | 企微 URL 验证 |
| `POST /assistant/wecom/callback` | 企微消息接收 |
| `WS /relay/ws` | Agent 连接（query `token` JWT 鉴权） |
| `GET /relay/channels` | 活跃渠道列表 |
| `WS /xiaozhi/ws` | XiaoZhi 设备连接 |
| `GET /health` | 健康检查（status + agent_connected + 各渠道开关） |

### 4.5 对 pandapal 的依赖

Relay **不依赖完整的 pandapal 包**。通过本地副本（`message_types.py`, `normalized_events.py`, `router_models.py`, `transport_protocol.py`）保持协议兼容。Relay 与本地 Backend 通过 WebSocket 松耦合通信。

> ⚠️ 协议副本靠**人工同步**：`normalized_events.py` 的 EventType 当前与 pandapal 逐字一致（46 成员），但无自动校验脚本；`transport_protocol.py` 头部已标注「抽共享包」技术债。改协议需同步两处。

---

## 五、跨项目关系总结

| 子项目 | 类型 | 部署位置 | 依赖关系 |
|--------|------|---------|---------|
| **pandaren** | SDK 核心库 | 嵌入 pandapal | 无上下游依赖 |
| **pandapal** | Agent 运行时后端 | 本地 sidecar | 依赖 pandaren |
| **pandapal_desktop** | Tauri 桌面前端 | 本地桌面 | 依赖 pandapal (via stdin/stdout IPC) |
| **pandapal_relay** | 云端 Relay Server | 云端 | 与 pandapal 通过 WebSocket 通信 |

---

## 六、常见任务指引

| 任务 | 从哪里开始 |
|------|-----------|
| **pandaren SDK 开发** | |
| 新增 Tool | `pandaren/tool/`（框架）+ 参考内置实现 `pandaren/tools/` |
| 新增 LLM Provider | `pandaren/llm/providers/` → 读 `ADDING_A_PROVIDER.md` + `_template.py.example` |
| 新增审计事件/Hook | `pandaren/observability/types.py` / `pandaren/hook/hooks.py` |
| 修改 8-Phase 循环 | `pandaren/engine/run_core.py` |
| 新增子 Agent | `pandaren/sub_agent/` + 内置提示词参考 `pandaren/agents/` |
| 自定义观测/记忆后端 | 实现 Protocol → 通过 `.observability()` / `.memory()` 注入 |
| **pandapal 后端开发** | |
| 新增路由消息类型 | `pandapal/messages/types.py`（RouterMessageType） |
| 新增 IPC 消息类型 | `pandapal/desktop_ipc/message_codec.py`（IpcMessageType）→ 同步 `pandapal_desktop/src/types/api.ts` |
| 新增 EventType | `pandapal/events/normalized.py`（注意会话级/全局级 scope 二分） |
| 新增子系统 | `pandapal/subsystem_registry.py` 添加 `SubsystemSpec`（声明 needs） |
| 新增工具 | `pandapal/tools/` → 参考现有模式（须注册进对应 *Tools 子系统） |
| 修改调度逻辑 | `pandapal/scheduler/`（pool/executor/manager 分层） |
| 修改 HITL 流程 | `pandapal/hitl/bridge.py` + `scheduler/hitl_manager.py` |
| 新增存储域 | `pandapal/storage/repositories/`（sqlite_* + markdown_* 双实现） |
| 代码改动收尾测试闭环 | `pandapal/local/TEST_RULE.md`（注入 coding system prompt；由 test-designer/test-coder 子 Agent 执行） |
| **pandapal_desktop 桌面端开发** | |
| 新增页面 | `pandapal_desktop/src/pages/` + `App.tsx` 添加路由 |
| 新增 IPC 消息类型 | `pandapal_desktop/src/types/api.ts` → 同步 `pandapal/desktop_ipc/message_codec.py` |
| 新增 Zustand store | `pandapal_desktop/src/store/`（22 个现有 store 参考） |
| 新增工具渲染器 | `pandapal_desktop/src/components/ChatArea/toolRenderers/` |
| 新增 Rust command | `pandapal_desktop/src-tauri/src/lib.rs`（或按域放入 auth.rs/workspace.rs/pets.rs） |
| **pandapal_relay 云端开发** | |
| 新增渠道 | `pandapal_relay/server.py` / `run_relay.py` 添加 WebSocket/HTTP 端点 |
| 修改企微消息处理 | `pandapal_relay/wecom_bridge.py` |
| 修改语音识别/合成 | `pandapal_relay/xiaozhi/asr.py` / `tts.py` |
| 新增 Auth 接口 | `pandapal_relay/auth/` |

---

## 七、禁止事项（全局适用）

- ❌ pandaren 的 `Identity` 不得运行时修改（HC1）
- ❌ pandaren 的 `Agent.run()` 不得向外抛异常（O3）
- ❌ 不得绕过 `AuditLog`（HC4）
- ❌ 不得使用裸 `dict` 跨层传递结构化数据
- ❌ SDK 内部（`pandaren/`）不得 import 应用层模块
- ❌ 修改 IPC 消息类型时，`pandapal/desktop_ipc/message_codec.py` 和 `pandapal_desktop/src/types/api.ts` 必须同步更新
- ❌ Relay 中的协议本地副本不得与 pandapal 主包产生不一致
- ❌ **ID / 决策 / 金额类字段缺失时给默认值**（`model_id`/`session_id`/`provider`/`plan_action`/费用 缺失即 fail-fast，绝不 `or 默认`；详见 §九）
- ❌ **`except: pass` / 中间层静默吞异常**（违反 O2/O3；只有故障隔离点可 catch，且必 log + 转错误；详见 §九）
- ❌ **违反下方「SESSION_ID 契约」任意一条**（跨会话数据污染是最严重的隐性事故）

---

## 八、SESSION_ID 契约（命根子 · 全局适用）

> session_id 是「数据归属」的唯一凭证。伪造/丢失/替代 = 跨会话污染，且**静默**（比崩溃更可怕）。
> 完整版 + 事故复盘 + Checklist 见设计文档 docs/design/session-id-契约.md（docs/ 未随仓库公开，本地保留）。

**唯一真相源**：后端所有 session_id 的 **创建/校验/断言** 必须经由 `pandapal/session_id.py`
（顶层零依赖模块）：`from pandapal import session_id as session_id_mod`。禁止在业务代码里散落
`uuid` 生成、`or ""`/`or "unknown"` 兜底、`session_id or other_id` 替代。

**十二条红线（精简）**：

1. **创建权独占** —— 只有「发起方」（前端建会话 / 定时任务 / relay 渠道）能创建；消费方/中间层绝不创建。
2. **0 容忍空值** —— 用时为空即报错（`session_id_mod.require`），不返回兜底值。
3. **消费只读** —— in-flight 的 session_id 不可改/换/造。
4. **显式二分** —— 每条消息要么会话级（必带）要么全局级（明确不带，payload 声明 `scope=global`），无暧昧中间态。
5. **零降级** —— 绝不 `get_or_create`/回退当前视图/`or "unknown"`。降级=污染。
6. **禁止替代** —— 不用语义不同的 id 兜底（`sid || currentView` ❌）；`content.sid` 与 `msg.sid`
   属同一真相两层信封，允许但**必须** `session_id_mod.assert_consistent` 断言一致。
7. **有据校验** —— resume/审批路径必须从权威记录（RunState 复合键 / owner 归属 / approval_id）
   校验「相等」才放行，而非信任入站。
8. **不可变** · 9. **创建即登记归属** · 10. **端到端透传不重新推导** · 11. **物理隔离不坍缩** ·
   12. **违反必留痕（warning/audit，绝不静默）**。

**各层落点**：前端 `invoke` 必带「消息所属会话」的 sessionId（非当前视图）+ 读 pending 状态的
`useCallback` 必须进依赖数组；Rust `current_session_id()` 纯读不 mint，会话级决策命令 session_id
必填报错；后端入站 `require`、resume `assert_consistent`+RunState 校验、出站每条会话级事件带
`payload["session_id"]`（`ipc_transport` 对漏带者 warning，靠 `EVENT_SCOPE_KEY` 区分真·全局事件）。

---

## 九、健壮性与降级契约（与 SESSION_ID 契约同级 · 全局适用）

> 静默降级 = 「结果不对但没报错」，比崩溃更难查（触发事故：`model_id` 静默回落默认模型撞额度）。
> **完整版 + 判据 + Checklist + CI 规则见设计文档 docs/design/健壮性与降级工程原则.md（docs/ 未随仓库公开，本地保留）。**
> 本契约是 SDK harness 原则（O2/O3/E4/E5/B5）在 pandapal 应用层的收敛与锐化。

**四个字段类别决定一切**（降级/默认/异常都按它判）：

| 类别 | 例子 | 规则 |
|------|------|------|
| **决策/门禁类** | `plan_action`、HITL `decision`、`is_pending` | 缺失即 **fail-closed / fail-fast**，绝不默认放行 |
| **ID / 身份类** | `session_id`、`user_id`、`model_id`、`provider` | 缺失即 **报错**，没有 default 这回事，绝不 `or 默认`/`or None` |
| **金额 / 计费类** | 价格、费用、预算 | 缺失绝不默认 **0**，必须 warning 留痕 + 记兜底桶 |
| **展示 / 辅助类** | 会话标题、preview、UI 文案 | 可回落 default，但**至少留痕** |

**五条红线（精简）**：

1. **默认值只在「定义处」声明一次**（函数签名/常量/dataclass）；**消费点禁止 `or 默认` / `.get(k,兜底)`**——那是散落兜底、贻害无穷的真身。
2. **前三类零默认**——决策/ID/金额缺失只能失败，别叫「降级」，叫「必须失败」。
3. **异常要么传播、要么在故障隔离点 catch-log-转错误**；中间层（锁查询/ID 解析/计费）**无 broad except**，`except: pass` 永远禁止（O2/O3）。故障隔离点白名单：`Agent.run` / `spawn_background` / 路由顶层 handler / 观测·账本 Fail-Safe 边界。
4. **降级必留痕 + 有熔断上限 + 只由 owner 层判一次**（防级联：model_id 回落→模型变→pricing 计 0→预算失效）。
5. **降级/兜底走统一 `pandapal.degradation` 通道**（log + Metrics counter，`event_code` 作主键），不散打 warning、不另造观测柱（复用四大观测支柱的 Metrics/Logger）。

**魔法数字**：命名 + 就近归属（跨模块→`constants.py`，单模块→模块内），契约字符串（`"model_id"` 等跨 Python/Rust/TS 键）优先收编（B5）。
