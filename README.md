# PandaPal Buddy

> **Enterprise-grade Python Agent framework — every step of your Agent is "visible and controllable".**

[简体中文](README.zh-CN.md) | English

PandaPal Buddy is a production-oriented Agent development framework covering the **full stack: core engine → runtime backend → desktop client → cloud relay**. It bakes observability, permission control, and human-in-the-loop (HITL) approval **into the Agent loop itself**, rather than bolting them on afterward.

The repository contains **four sub-projects** that work together:

| Sub-project | Layer | Description |
|-------------|-------|-------------|
| [`pandaren/`](pandaren/) | SDK core library | Engine, security, memory, observability. No upstream/downstream dependencies |
| [`pandapal/`](pandapal/) | Runtime backend | Agent application layer: IoC container + declarative subsystems, connects multiple channels |
| [`pandapal_desktop/`](pandapal_desktop/) | Desktop frontend | Tauri v2 + React 18, talks to backend over stdin/stdout IPC |
| [`pandapal_relay/`](pandapal_relay/) | Cloud relay (optional) | FastAPI service, integrates WeCom (企业微信) and XiaoZhi smart speakers |

```
┌─────────────────────────────────────────────────────────────┐
│  pandapal_desktop            Tauri v2 Desktop Frontend      │
│  (React + TypeScript)        UI, streaming typewriter, HITL │
└──────────────────────────┬──────────────────────────────────┘
                           │ stdin/stdout (IPC JSON Lines)
┌──────────────────────────▼──────────────────────────────────┐
│  pandapal                    Agent Runtime Backend (sidecar) │
│  (Python)                    routing, scheduling, HITL mgmt  │
│                              depends on pandaren engine      │
└──────────────┬───────────────────────────┬──────────────────┘
               │                           │ WebSocket
┌──────────────▼──────────┐  ┌─────────────▼──────────────────┐
│  pandaren                │  │  pandapal_relay               │
│  Agent SDK core (Python) │  │  Cloud Relay Server (FastAPI) │
│  engine/security/memory  │  │  WeCom + XiaoZhi smart speaker│
└──────────────────────────┘  └────────────────────────────────┘
```

---

## 🎯 Why PandaPal Buddy?

Most Agent projects hand you a library or a demo. PandaPal Buddy hands you **a desktop AI assistant you can use today** — and opens the source of every layer underneath it. Above all, it lives by one conviction:

> **An Agent is only trustworthy when every step is visible, and every risky action is controllable.**

### 1️⃣ A desktop assistant, not just a framework

**Three core capabilities, distilled:**

- **🧭 Autonomous execution loop** — one instruction in, a multi-step job done: task orchestration, multi-agent delegation, plan mode and user-defined skills turn a single prompt into a full workflow
- **👁️ Total visibility** — the Dashboard shows budgets, sessions and agent activity at a glance; AI diffs surface every file change; reasoning streams in live. Nothing happens in the dark
- **🛑 Human control where it matters** — HITL approvals pause risky actions until a human decides; plan mode requires approval before any code runs; scheduling keeps your agent working around the clock

On par with mainstream AI coding assistants (Claude Code / Codex / WorkBuddy), ready for **both coding and office scenarios** — with a full desktop UI (Tauri v2 + React) instead of a bare CLI.

### 2️⃣ Open source, end to end — zero black boxes

Every layer of the stack is public, from the cloud server to the engine kernel:

| Layer | Project | What you get |
|---|---|---|
| Cloud server | [`pandapal_relay/`](pandapal_relay/) | FastAPI relay: WeCom + smart-speaker access, account/auth system |
| Runtime backend | [`pandapal/`](pandapal/) | Agent application layer: IoC container, routing, scheduling, HITL |
| SDK core | [`pandaren/`](pandaren/) | The engine itself: 8-phase ReAct, security, memory, observability |
| Desktop client | [`pandapal_desktop/`](pandapal_desktop/) | Tauri v2 + React GUI, streams the whole agent lifecycle in front of you |

Nothing is closed-source. Wire protocol, engine internals, UI — read it, change it, run it yourself.

### 3️⃣ Visible & Controllable — observability as a product

Observability is **baked into the loop**, not bolted on as a plugin. The **Dashboard** turns agent runs into inspectable timelines (tokens, tools, costs, approvals), and the engine enforces the rest in code:

- `AuditLog` **cannot be disabled** on any code path — every run leaves a complete audit trail
- `Identity` is physically immutable (`__slots__` + `__setattr__`) — no runtime privilege escalation
- Circuit breaker halts the loop the moment a threshold trips
- A 12-rule `session_id` contract makes cross-session data pollution impossible by construction

**At a glance** — 4 open-source sub-projects · 8-phase ReAct loop · 17 streaming events · 46 normalized event types · 21 lifecycle hooks · 11 pluggable extension points · 4 observability pillars

---

## ✨ Core Features

**Engine core (`pandaren/`)**

- **8-Phase ReAct Loop**: context prediction → message building → LLM call → output parsing → tool selection → tool execution → result collection → decision making, unified in a single execution core (`RunCoreMixin._run_stream_core()`)
- **Security built-in**: `Identity` is physically immutable (`__slots__` blocks modification), `AuditLog` cannot be disabled, `PermissionGuard` gates access, circuit breaker halts on threshold
- **Human-in-the-Loop (HITL)**: high-risk operations pause in real time, wait for human approval, then resume — fully auditable, available on the desktop client and WeCom (via the relay server)
- **Four Observability Pillars**: AuditLog / Tracer / Metrics / Logger cut across the whole pipeline; 17 streaming event types, 21 lifecycle hooks
- **Three-tier Memory**: short-term / long-term / working memory + a 4-stage compression pipeline with precise token budget control
- **Multi-Agent Delegation**: SubAgent registry + blueprints to decompose complex tasks
- **11 Pluggable Extension Points**: memory backends, observability backends, compression policies, token estimators... all Protocol-based, fully replaceable
- **Plan / AskUser / HITL** — a full range of human-AI interaction modes
- **46 streaming event types** — fully supported end to end
- **Mainstream model support** — Anthropic / DeepSeek / DashScope / Volcengine

**Runtime & desktop (`pandapal/`, `pandapal_desktop/`, `pandapal_relay/`)**

- **Dashboard** — budgets, sessions, agent activity at a glance
- **AI diff review** — every file edit shown as an inline Monaco diff; nothing hidden
- **Task scheduling** — cron / event / manual triggers; it keeps working while you sleep
- **Budget quotas** — per-provider accounting; halt the moment a quota is hit
- **WeCom & smart-speaker channels** — approval cards inside WeChat Work, or talk to your agent via a XiaoZhi speaker *(Impress channel under development)*
- **PetDex desktop pet** — agent activity → pet animation feedback

---

## 🗺️ Capability Map

> ★ = the capabilities we consider the most differentiated.

### `pandaren/` — Engine Core (SDK)

| Capability | What it does |
|---|---|
| ★ 8-Phase ReAct loop | One execution core: context prediction → message building → LLM call → output parsing → tool selection → tool execution → result collection → decision |
| ★ Physically immutable `Identity` | `__slots__` + `__setattr__` + `__delattr__` block every runtime mutation — no privilege escalation |
| ★ TrustLevel grading | EXTERNAL / SUB_AGENT / ORCHESTRATOR — sub-agents can never outrank their orchestrator |
| ★ Uncloseable `AuditLog` | 21 audit event types; no code path can bypass the audit trail |
| PermissionGuard | Intercepts high-risk operations by sensitivity (LOW → CRITICAL) |
| Circuit breaker + Harness | Halts the loop on threshold; step-count / timeout / context-budget guards |
| HITLController | Pause high-risk actions → human decision → resume |
| Multi-provider LLM | OpenAI-compatible protocol + Qwen / Doubao / DeepSeek plugins; LLMRouter |
| Two-tier tool registry | ALWAYS (≤15 resident) / DEFERRED (lazy-loaded) — controls token cost |
| ★ Skill injection | Plain-Markdown skills with auto-trigger control |
| ★ Three-tier memory | Short/long-term/working memory + 4-stage compression pipeline + token budget control |
| ★ Four observability pillars | AuditLog / Tracer (8 span types) / Metrics / Logger; 4 swappable backends |
| ★ 21 lifecycle hooks | From run to skill — the whole lifecycle is extensible |
| ★ 11 pluggable extension points | Memory backends, observability backends, compression policies, token estimators… all Protocol-based |
| 17 streaming event types | Including REASONING_TOKEN (visible reasoning) and PLAN_APPROVAL_REQUESTED |
| Plan Manager | Plan → approve → execute at engine level |
| Multi-agent delegation | SubAgent registry + blueprints |
| Cancellation | CancelToken — abort any run on demand |
| AgentBuilder fluent API | 16 chained methods; a fully-equipped agent in ~20 lines |

### `pandapal/` — Runtime Backend

| Capability | What it does |
|---|---|
| ★ IoC container | 17 declarative subsystems; automatic topological ordering + DI; one failing subsystem doesn't block the rest |
| Message routing | Parse / dedupe / route; 9 route vocabularies |
| Streaming execution engine | AgentExecutor — run_stream yields every event live |
| ★ Session concurrency pool | Multi-session concurrency + queued/started/released states + eviction |
| ★ Single HITL state owner | HITLBridge prevents race conditions; ask_user questionnaires separate from approvals |
| ★ Cross-channel broadcast | One event → desktop / WeCom / speaker; per-channel policy |
| ★ 46 normalized event types | Explicit session-level vs global-level scoping |
| ★ session_id contract | 12 rules — cross-session pollution is impossible by construction |
| ★ Budget quotas | Per-provider accounting; halt on quota hit, not after-the-fact |
| Persistence | SQLite + Markdown dual backends; 10+ repositories (audit/session/task/working memory…) |
| Task scheduling | cron / event / manual triggers |
| Dashboard aggregation | Feeds the desktop Dashboard |
| Agent task tools | Task decomposition / progress / result collection |
| Web search & fetch | Built-in web tools |
| Unified degradation channel | Every degradation leaves a trace (log + Metrics) — never silent |
| Background isolation | spawn_background — failures don't touch the main flow |

### `pandapal_desktop/` — Desktop Client (Tauri + React)

| Capability | What it does |
|---|---|
| ★ AI diff review | Monaco inline diff for every file change (standalone sub-project) |
| ★ Streaming typewriter + reasoning | Tokens and REASONING_TOKEN rendered live |
| ★ Tool-call visualization | Dedicated renderers per tool (bash/edit/read/write/web…) |
| ★ Desktop pet | Agent activity → pet animation feedback |
| HITL / Plan / ask_user modals | All three pause types covered |
| File renderers | HTML / image / Markdown / PDF / table / log inline preview |
| Dashboard page | Budget / sessions / agent activity aggregation |
| Skill management + editor | Visual Skill CRUD |
| Task scheduling page | Visual cron management |
| BYOK credentials | Bring-your-own-key + model config wizard |
| ⌘K command palette | Global search |
| File explorer / workspace | File tree + workspace switching |
| Session management | Groups / favorites / history / concurrency control |
| 22 Zustand stores | Cleanly separated state domains |

### `pandapal_relay/` — Cloud Relay

| Capability | What it does |
|---|---|
| ★ WeCom integration | Message callbacks + HITL approval cards inside WeChat Work |
| ★ XiaoZhi smart speaker | Voice: Opus buffering + ASR + TTS (swappable backends) |
| Reliable WebSocket channel | JWT auth, 50-message offline buffer, ACK delivery, heartbeat |
| Account system | Register-to-login, JWT, anti-enumeration/lockout, refresh grace |
| Fail-fast config | Refuses to boot with incomplete config |

---

## 📦 1. `pandaren/` — Agent SDK Core Library

The foundation of everything: a pure-Python, production-oriented Agent engine. It has **no upstream/downstream dependencies** and can be used standalone via `pip install pandapal-buddy`.

### Four-layer Architecture

```
Layer 4: engine/         AgentLoop (8-Phase ReAct), message building, output parsing, streaming events
Layer 3: behavior/       PermissionGuard, HITLController, Harness (runtime protection)
Layer 2: capability/     llm/ + tool/ + skill/ + memory/
Layer 1: identity/       Identity, Permission, TrustLevel (immutable foundation)

Cross-cutting: observability/     AuditLog, Tracer, Metrics, Logger (four observability pillars)
Cross-cutting: hooks.py           AgentHooks (21 unified lifecycle extension points)
```

Dependency direction is strictly one-way: `engine → behavior → capability → identity`

### Quick Start

**Prerequisites**: Python ≥ 3.12

```bash
pip install pandapal-buddy
# Optional extras
pip install pandapal-buddy[all]   # full features (PDF/image/trash/validation/tokenizer/testing)
```

**Minimal Agent**:

```python
from pandaren.builder import AgentBuilder
from pandaren.llm import OpenAICompatibleClient

agent = (
    AgentBuilder()
    .identity(agent_id="my_agent", agent_name="assistant")
    .llm(client=OpenAICompatibleClient.for_openai(api_key="sk-..."))
    .llm_settings(temperature=0.7, max_tokens=4096)
    .tools([search_web, calc])          # register tools (two-tier: ALWAYS/DEFERRED)
    .skills([domain_knowledge])         # inject domain knowledge
    .system_prompt("You are a helpful assistant...")
    .behavior(max_steps=30, total_timeout=300.0)
    .observability()                    # default audit + tracing
    .build()
)

result = await agent.run("What's the weather today?", session_id="session-001")
# result: AgentResult — never raises; all exceptions are converted internally (O3)
```

**Module Reference**:

| Module | Core classes | Responsibility |
|--------|--------------|----------------|
| `pandaren/identity/` | `Identity`, `Permission`, `TrustLevel` | Immutable identity declaration, permission sets |
| `pandaren/llm/` | `OpenAICompatibleClient`, `ModelSettings` | Multi-provider LLM clients (OpenAI/Qwen/Doubao/DeepSeek) |
| `pandaren/tool/` | `Tool`, `ToolTier`, `ToolRegistry` | Tool registration, two-tier classification |
| `pandaren/behavior/` | `PermissionGuard`, `HITLController`, `HarnessExecutor` | Permission guard, HITL approval, circuit breaker |
| `pandaren/engine/` | `AgentLoop`, `RunCoreMixin`, `MessageBuilder` | 8-Phase ReAct core |
| `pandaren/memory/` | `Memory`, `ShortTermMemory`, `LongTermMemory` | Three-tier memory, 4-stage compression pipeline |
| `pandaren/hook/hooks.py` | `AgentHooks`, `DefaultAgentHooks` | 21 lifecycle extension points |
| `pandaren/observability/` | `AuditLog`, `Tracer`, `Metrics`, `Logger` | Four observability pillars |
| `pandaren/builder.py` | `AgentBuilder` | Fluent API entry point |

---

## 📦 2. `pandapal/` — Agent Runtime Backend

The application layer that turns the pandaren engine into a **complete, runnable Agent service**: it connects the engine to desktop clients, WeCom, smart speakers, and other channels.

### Key Capabilities

- **IoC container** (`SubsystemContainer`): automatic topological ordering + dependency injection for 11+ subsystems declared in `subsystem_registry.py`
- **Message routing** (`MessageRouter`): parse, deduplicate, route inbound messages
- **Agent scheduling** (`AgentScheduler` + `AgentExecutor`): pure routing + streaming execution engine
- **HITL management** (`HITLManager` + `HITLBridge`): pause/resume approval flows; `HITLBridge` is the single owner of approval state
- **Cross-channel broadcast** (`MessageBroadcast` + `ChannelRegistry`): dispatch normalized events to every connected channel
- **Desktop IPC** (`StdioIpcServer` + `IpcStdoutTransport`): JSON Lines over stdin/stdout for the Tauri desktop client
- **Persistence** (`StorageManager`): SQLite-backed, 10+ repositories
- **Task scheduling** (`TaskScheduler`): cron / event / manual triggers

### Message Flow (end to end)

```
[Desktop client] ──stdin JSON──→ StdioIpcServer ──→ InboundMessage
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
                   [Desktop client]  [WeCom]       [Relay Server]
```

### Quick Start

```bash
pip install -e ".[desktop]"       # install pandapal + dependencies
# `pandapal.local` is the desktop sidecar entry; it is normally launched
# automatically by the Tauri client. To run it manually:
python -m pandapal.local --workdir <workspace_dir> --app-data-dir <app_data_dir>
```

---

## 📦 3. `pandapal_desktop/` — Desktop Client

A **Tauri v2 + React + TypeScript** desktop GUI for interacting with the Agent locally — streaming typewriter replies, HITL approval modals, Plan Mode approval, task progress panels and more.

### Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | React 18 + TypeScript + Vite 5 |
| Desktop shell | Tauri v2 (Rust) |
| State management | Zustand 4 |
| Routing | React Router DOM 6 |
| Code editor | Monaco Editor (`@monaco-editor/react`) |
| IPC | `@tauri-apps/api` (invoke + event listen), **no WebSocket** |

### How It Talks to the Backend

```
User → React frontend
        ↓ invoke("send_message", ...)
       Rust (Tauri Core)
        ↓ write stdin
       Python Sidecar (PandaPal Agent)
        ↓ stdout "IPC:{json}"
       Rust → emit("backend-event", json)
        ↓ listen("backend-event")
       React frontend (BackendProvider dispatches)
```

The Python sidecar is packaged with **PyInstaller (onedir mode)** and launched by Rust via `std::process::Command`.

### Development

**Prerequisites**: Python ≥ 3.12, Node.js ≥ 18 + pnpm, Rust toolchain

```bash
cd pandapal_desktop
pnpm install

pnpm tauri:dev       # build Python sidecar + launch full desktop app (recommended)
pnpm dev             # Vite frontend only (browser preview)
pnpm tauri build     # package installers (Windows: .exe / macOS: .app/.dmg)
pnpm typecheck       # TypeScript type checking
```

---

## 📦 4. `pandapal_relay/` — Cloud Relay Server (optional)

An independently deployable **FastAPI service** that bridges external channels to your local Agent over a WebSocket connection.

> **Optionality note**: The Relay server and its channels are all **optional** — the desktop app runs fully local and supports local login, with no cloud component required. Enable channels on demand:
>
> - **WeCom (企业微信)**: optional — enable it only when you want your Agent reachable from WeChat Work;
> - **XiaoZhi smart speaker**: still under development — stay tuned.

### Key Capabilities

- **Agent WebSocket access**: `WS /relay/ws` — a single Agent connects; supports 50-message offline buffering and drain-on-reconnect
- **WeCom integration**: `POST /assistant/wecom/callback` — XML parsing, AES decryption, dedup, async forwarding; also serves as a HITL approval channel with template cards
- **XiaoZhi smart speaker**: `WS /xiaozhi/ws` — Opus audio frame buffering, ASR speech recognition, TTS speech synthesis
- **Account system**: REST register / login / password change, bcrypt hashing, JWT signing, SQLite persistence
- **Multi-channel reply routing**: chain dispatch (XiaoZhi → WeCom → fallback)

### API Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /auth/register` | User registration |
| `POST /auth/login` | User login |
| `PUT /auth/password` | Change password |
| `POST /assistant/wecom/callback` | WeCom message callback |
| `WS /relay/ws` | Agent connection |
| `WS /xiaozhi/ws` | XiaoZhi device connection |
| `GET /health` | Health check |

### Quick Start

```bash
cd pandapal_relay
pip install -e ..                 # editable install from the repository root (all deps)
# configure .env (see pandapal_relay/.env.example)
python -m pandapal_relay         # listens on 0.0.0.0:8090
curl http://localhost:8090/health
```

---

## 📖 Documentation

- [PANDAPAL.md](PANDAPAL.md) — Project overview and technical architecture (all four sub-projects, module reference, design principles)
- [README.zh-CN.md](README.zh-CN.md) — 简体中文版 README

## 🧪 Testing

```bash
pip install -e ".[dev]"
pytest                       # collects all tests under pandapal/, pandaren/, scripts/
```

Code style is enforced with [ruff](https://github.com/astral-sh/ruff) (configured in `pyproject.toml`).

## 🤝 Contributing

Issues and Pull Requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

## 🔒 Security

Found a security vulnerability? Report it privately through the channels described in [SECURITY.md](SECURITY.md). Please do not disclose it publicly.

## 📄 License

[MIT](LICENSE) © 2026 keanezhang
