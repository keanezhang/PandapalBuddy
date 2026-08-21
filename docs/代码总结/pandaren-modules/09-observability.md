# pandaren/observability — 可观测层（四大观测支柱）

> 模块：pandaren/observability | 生成：2026-08-19 @ git `95092f3` | 锚点以生成时点代码为准

---

## 1. 模块定位与职责（存在的意义）

**为什么存在**：让 Agent 的每一步都「看得见」。Loop 的 8-Phase ReAct 循环内部发生什么，必须能被审计、被追踪、被量化、被记录——否则引擎就是黑盒：合规无法证明、性能无法定位、事故无法复盘。

**不建它会怎样**：Agent 引擎跑完一次 run，你只知道「它返回了结果」，不知道它调了几次 LLM、每步耗时多少、工具执行卡在哪里、有没有被拒绝的高风险操作。生产事故（如一次 LLM 调用 3 秒）无法定位，合规审计（谁在何时批准了什么）完全缺失。

**角色分工图**：

```
┌─────────────────────────── observability 层 ───────────────────────────┐
│  4 个 Facade（对外 API 门面）                                            │
│    AuditLog（HC4 强一致）│ Tracer │ Metrics │ Logger                    │
│   + ObservabilityHooksAdapter（Loop 桥接，最大文件）                     │
│   + ObservabilityProvider（工厂） + DefaultSanitizer（脱敏）            │
│   + Protocols（5 个替换扩展点） + Config（显式四态）                     │
│   └─ backend/：Console / InMemory / Markdown / SQLite（4×4=16 个后端类）│
└──────────────────────────────────────────────────────────────────────────┘
```

**覆盖文件清单**（源码 13 个 + 后端 5 个，不含 tests）：

| 文件 | 大小 | 职责 |
|------|------|------|
| `types.py` | 10.8KB | 枚举（AuditEventType×21 / SpanType×8 / LogLevel / TraceLevel / SpanStatus）+ 不可变数据结构（AuditRecord / Span / ObservabilityContext）|
| `protocols.py` | 2.0KB | 5 个替换扩展点 Protocol（Audit / Tracer / Metrics / Logger / Sanitizer）|
| `config.py` | 2.5KB | ObservabilityConfig frozen 值对象（显式四态）|
| `audit.py` | 7.4KB | AuditLog Facade（HC4 核心，写失败传播）|
| `tracer.py` | 11.4KB | Tracer Facade（span 树生命周期 + trace 上下文传播）|
| `metrics.py` | 10.6KB | Metrics Facade（通用 API + 命名便捷 API）|
| `logger.py` | 3.4KB | Logger Facade（结构化 JSON 记录）|
| `sanitizer.py` | 3.6KB | DefaultSanitizer（正则脱敏，字段级优先）|
| `hooks_adapter.py` | 37.4KB | ★ Loop Hooks 桥接器（19 个 hook 方法 → trace+metrics+logs）|
| `provider.py` | 8.1KB | ObservabilityProvider 工厂（四态解析 + Null 后端）|
| `exceptions.py` | 0.5KB | ObservabilityError / AuditWriteError（独立基类）/ SanitizeError |
| `backend/`（5 文件） | 69.5KB | 4 套存储后端 × 4 子系统 + 导出 |

**测试清单**（`tests/` 5 文件 + 1 设计文档）：

| 文件 | 大小 | 覆盖 |
|------|------|------|
| `test_observability.py` | 34.4KB | Facade 层功能测试 |
| `test_observability_mock.py` | 63.3KB | ★ 最大测试，Mock 引擎全链路 |
| `test_data_isolation.py` | 16.4KB | 会话数据隔离（session 分片）|
| `test_sqlite_backend.py` | 12.5KB | SQLite 后端 CRUD |
| `test_sqlite_read_defense.py` | 27.3KB | SQLite 读防御（对应 design/sqlite_read_defense.design.md）|

---

## 2. 方案总览（产品视角）

> 非技术读者可只读本节：什么场景下解决什么问题 + 总体思路。

### 2a. 在什么场景下解决什么问题（场景穷举）

| 场景 | 已有/缺失 | 该场景下的问题 |
|------|----------|---------------|
| 合规审计：谁在什么时候执行了什么（含拒绝的操作） | 已有 | 审计必须可证明、不可关闭（HC4），写失败宁可报错也不静默丢 |
| 性能排查：一次 run 哪一步最慢、LLM 调用耗时分布 | 已有 | span 树带时间戳，run→step→llm/tool 层级可见 |
| 实时监控：运行轮数、步数、工具调用数、活跃 run 数 | 已有 | 命名指标 API + gauge/counter/histogram 三型 |
| 结构化排障日志：run/step/llm/tool 生命周期事件 | 已有 | 每条记录带 run_id/session_id/step_n，可按会话分片落盘 |
| 敏感数据脱敏：API key、token 不进观测存储 | 已有 | 字段级正则脱敏，脱敏失败用占位符不崩溃 |
| 多 Agent 协作链路追踪 | 已有 | ObservabilityContext 传播 trace_id/parent_span_id |
| 跨 Agent 成本核算（token 费用） | 缺失 | SDK 明确不计价：只记 token/命中等事实，金额由应用层自算（代码注释为据，见 provider.py:82）|
| 全链路实时流式看板（事件推送） | 缺失 | 后端是「写后查询」模型，无推送；实时看板由应用层 pandapal 聚合（events/normalized.py）|

### 2b. 总体方案思路（策略）

1. **四支柱分层，强弱二分**：AuditLog 强一致（HC4，写失败传播到 Loop），Tracer/Metrics/Logger Fail-Safe（降级不传播）——同一层内两种容错哲学，各自有明确适用边界。
2. **Loop 不直接碰 Facade**：引擎只触发 AgentHooks 生命周期点，由 ObservabilityHooksAdapter 统一桥接到三套观测（见 §5.4），审计则硬编码调用（不可绕过的双通道设计）。
3. **后端全可插拔**：Protocol 定义接口，4 套实现（Console 调试 / InMemory 测试 / Markdown 人工可读 / SQLite 生产），存储位置与格式与观测逻辑解耦。
4. **显式四态配置**：`不传=关`、`False=关`、`"mem"=内存`、`实例=自定义`——消除「默认开了没」的歧义，Audit 例外（HC4 不可关，False 降级 InMemory + WARN）。
5. **事件流 vs 聚合快照分离存储**：Audit/Log/Trace 是「事件流」按 session 分片，Metrics 是「聚合快照」全局单文件（见 §5.6）。

---

## 3. 产品视角

### 3a. 使用场景与用户旅程

**谁**：Agent 框架开发者（用 AgentBuilder 装配）、Agent 运维者（排查生产问题）、安全/合规人员（审计取证）。
**典型旅程**：开发者 `.observability(audit=SQLiteAuditBackend("./obs.db"), tracer="mem")` 构建 Agent → 跑一次 run → 查询 span 树看每步耗时 → 翻 audit 记录确认高危操作有留痕 → 并发压测时用 active_runs gauge 观察并发水位。

### 3b. 量化价值与反面案例

- **反面案例 1（并发串扰）**：多会话共享同一 HooksAdapter，若 span 缓冲不按 run_id 隔离，A 会话的 tool span 会挂到 B 会话的 run span 下——排查时「链路串台」，比没有链路更误导。已修复（git 87aea2c：按 run_id 隔离 span 缓冲）。
- **反面案例 2（active_runs 虚高）**：plan 模式 run 结束后若不清 `_active_run_ids`，活跃数单调上涨永不清零，监控面板显示「8 个活跃 run」实则 0 个（历史事故，hooks_adapter.py:164-168 注释为据）。
- **反面案例 3（暂停被记错）**：HITL/交互等待被当作失败记录 error span，事后统计误报「50% run 失败」，实际是人工审批中的正常挂起。
- **量化收益**：每次 run 产生一棵带毫秒级 duration 的 span 树（step/llm/tool 全粒度）；每个工具调用、每次 LLM 调用都有 counter 可聚合；每次拒绝/审批都有不可关闭的审计记录。

### 3c. 产品地图定位

能力域：pandaren SDK 横切层（engine → behavior → capability → identity 之外的第 5 条横切线）。
上游：engine（AgentHooks 事件）、behavior（PermissionGuard/HITLController 审计调用）、tool/skill（注册事件）、identity（agent_id）。
下游：4 套存储后端；被 pandapal 应用层消费（看板聚合、会话列表）。

### 3d. 能力边界与承诺

**能承诺（代码强制）**：
1. 审计不可关闭、不可采样（HC4，audit.py:98-103 拒绝 None 后端）。
2. 观测子系统故障不传播异常到 Loop（tracer/metrics/logger 全程 try/except 降级）。
3. span 属性/日志内容按 sanitizer 规则脱敏后才落存储（tracer.py:225-239）。
4. 会话级数据按 session_id 物理分片，无归属落 `_no_session` 兜底桶。

**明确不做**：
1. 不计算/记录 token 费用（SDK 不计价，金额归应用层）。
2. 不做实时事件推送（写后查询模型，流式看板由应用层做）。
3. 不防提示词注入、不保证后端存储本身的加密（脱敏在写入前，静态加密是存储层职责）。

### 3e. 用户视角的失败体验

| 技术风险 | 用户看到 |
|---------|---------|
| 审计后端写失败（磁盘满/文件损坏） | run 直接报错（AuditWriteError 传播到 Loop），同时 stderr 打一条 JSON fallback——宁可失败也不静默丢审计 |
| tracer/metrics/logger 后端挂掉 | 用户无感知（debug 级留痕，不打断 run）——观测降级，业务不受影响 |
| 脱敏器抛异常 | 写入端用 `{"_sanitize_error": true}` 占位（tracer.py:238-239）——观测数据可能脱敏失败但不会崩溃 |
| 忘记配置 audit 后端 | 默认 InMemory：功能正常但重启丢数据（provider.py:166-170 显式 WARN 提示生产必须持久化） |

### 3f. 成熟度与演进路线

当前状态：**演进中**（已有真实事故修复历史：并发串扰、active_runs 泄漏、暂停误记 error，均有代码注释为据）。
已知演进方向：SQLite 读防御（tests/design/sqlite_read_defense.design.md 设计文档在案）；多会话并发安全已加固（git 87aea2c）。

---

## 4. 模块整体框架

```
                          AgentLoop（8-Phase ReAct，engine/run_core.py）
                                    │
                ┌───────────────────┴───────────────────┐
                │ 触发 21 个 AgentHooks 生命周期点       │ 硬编码 AuditLog.write_sync
                ▼                                       ▼（engine/behavior/tool 内直接调）
        ┌───────────────────┐                  ┌──────────────────┐
        │ ObservabilityHooks │                 │ AuditLog         │
        │ Adapter（桥接层）  │                 │ （HC4 强一致）    │
        │ 19 个 hook 方法    │                 │ 写失败→异常传播    │
        └──────┬──────┬──────┘                 └────────┬─────────┘
               │      │                                  │
        ┌──────▼─┐ ┌─▼────────┐                 ┌───────▼────────┐
        │ Tracer │ │ Metrics  │                 │ AuditBackend   │
        │ span树 │ │ counter/ │                 │ Console/InMem/ │
        └────┬───┘ │ hist/    │                 │ Markdown/SQLite│
             │     │ gauge    │                 └────────────────┘
        ┌────▼───┐ └────┬─────┘
        │ Logger │      │
        └────┬───┘      │
             │          │
        ┌────▼──────────▼──────────────────┐
        │ TracerBackend / MetricsBackend / │
        │ LoggerBackend                     │
        │ Console / InMemory / Markdown / SQLite │
        └──────────────────────────────────┘
```

**读图要点**：
1. **双通道**：Loop 观测走「Hooks → Adapter → 3 Facade」（可降级），审计走「硬编码 → AuditLog」（不可降级）——两条通道的容错语义完全不同。
2. **门面边界**：engine 只碰 `hooks` 对象和 `audit_log` 两个引用（builder.py:825 组装），不感知任何后端实现。
3. **后端可替换**：Facade 与存储完全解耦，切换 Console→SQLite 只改一个构造参数。

---

## 5. 核心机制详解

### 5.1 HC4 强一致审计：不可关闭、失败必报

**痛点**：审计是最容易「静默丢失」的数据——没人检查它是否真的写进去了，出事才想起来。
**机制**：`AuditLog` 不提供 `disable()`；backend 为 None 直接 `ValueError`（audit.py:99-103）；`write_sync` 在 `threading.Lock` 内完成 write+flush 原子两步（audit.py:147-149），任何异常 → 先打 stderr JSON fallback（`_write_fallback`）再抛 `AuditWriteError` 传播到 Loop；用户显式传 `False` 也不允许关闭，降级 InMemory + WARN（provider.py:164-171）。
**收益**：审计要么成功落盘、要么让 run 失败——绝不「以为记了其实没记」。
**代码事实**：audit.py:98-103（拒绝 None）、146-155（原子写+fallback+raise）、provider.py:164-171（False 降级 WARN）。

### 5.2 显式四态配置：用 `_UNSET` 哨兵消除歧义

**痛点**：传统布尔开关无法表达「未配置」和「显式关闭」，框架常偷偷默认开启一堆观测拖慢 CI。
**机制**：Builder 用 `_UNSET = object()` 哨兵（builder.py:92），`.observability()` 不传参 → 全关（零噪音）；传 `"mem"` 显式开内存后端；传实例用自定义后端；`False` 显式关。四态在 `provider.py` 统一解析，False/None → Null 后端（静默 no-op，provider.py:42-53）。
**收益**：测试/CI 零观测开销，生产按需精细开启，行为完全可预测。
**代码事实**：config.py:35-64（frozen 四态字段）、provider.py:42-53（Null 后端）、provider.py:91-177（四态解析）。

### 5.3 TraceLevel 三态记录策略

**痛点**：FULL 追踪在长 run 下产生海量 span，存储和查询成本高。
**机制**：`_should_record`（tracer.py:205-223）——FULL 全记；SUMMARY 全记但 end_span 时把超长字符串属性截断到 200 字符（tracer.py:124-127）；MINIMAL 只记 RUN 级 span + 异常 span，`mark_span_error` 强制异常 span 兜底记录（tracer.py:152-161）。
**收益**：默认 SUMMARY 平衡信息量与体积；MINIMAL 给极高吞吐场景兜底，且异常永不丢。
**代码事实**：tracer.py:205-223、124-127、152-161。

### 5.4 run_id 隔离缓冲 + 兜底关闭（多会话并发安全）

**痛点**：HooksAdapter 实例被多 session 共享（同一 Agent 多会话并发），span 缓冲若不按 run_id 隔离，A 会话的 step 会挂到 B 会话的 run 下——链路串台（git 87aea2c 修复的真实事故）。
**机制**：所有 per-run 状态按 run_id 分桶（`_run_spans[run_id]`、`_tool_spans_by_run[run_id]`、`_llm_spans[run_id]`、`_step_spans[run_id]`）；`on_run_end` 兜底关闭该 run 全部未关闭的子 span（Bug 3 修复，hooks_adapter.py:178-200），并清理全部 per-run 缓冲防 dict 泄漏（hooks_adapter.py:218-226）。
**收益**：并发多会话链路互不污染；异常中断的 run 不留悬挂 span、不泄漏内存。
**代码事实**：hooks_adapter.py:178-200（兜底关闭）、218-226（缓冲清理）、144-174（active_runs 精确释放）。

### 5.5 暂停语义不记错误（`_PAUSE_REASONS`）

**痛点**：HITL/交互等待是「正常挂起」不是「失败」，但历史实现把暂停 run 记为 error，统计误报「50% run 失败」。
**机制**：`_PAUSE_REASONS = {hitl_paused, interaction_paused, plan_complete}`（hooks_adapter.py:37-40）。`on_run_end` 中：暂停且未成功 → metric 记 `paused`、run span 记 `CANCELLED`；暂停且成功（plan_complete）→ 记 OK 且释放 active_runs（hooks_adapter.py:164-174 详注）；`hitl_rejected`/`cancelled` 这类人工拒绝才记 ERROR（hooks_adapter.py:208-209）。
**收益**：监控统计不再被审批等待污染，暂停/成功/失败三态可区分。
**代码事实**：hooks_adapter.py:37-40、149-162、206-211。

### 5.6 事件流 vs 聚合快照的存储判据

**痛点**：同一套后端 API，Metrics 与 Audit 的存储形态天差地别——Metrics 是不断覆盖的聚合值，Audit 是只追加的事件流，混在一起会互相拖累。
**机制**：判据写在 markdown.py:18-24——「一条记录是事件流还是聚合快照」：Audit/Logger/Tracer 是事件流 → 按 session_id 物理分片（`sessions/{sid}/audit.md` 等），无归属落 `_no_session/`；Metrics 是聚合快照 → 全局单文件不分片。SQLite 同理：4 张表（audit_records/spans/metrics_points/logs，sqlite.py:66-133）。
**收益**：per-file 锁不互相阻塞高频写；按会话取日志 O(1) 定位；指标全局聚合天然一致。
**代码事实**：markdown.py:6-24（存储结构注释）、41-50（`_sanitize_component` 路径防逃逸）、sqlite.py:66/87/112/127（四表）。

### 5.7 工具集指纹：Prefix Cache 命中观测

**痛点**：LLM 调用带工具列表（tool schema），工具列表变化会导致 provider 侧 prefix cache 失效——重复付全量 prefill 钱，但 SDK 侧看不见命中率。
**机制**：`on_before_llm_call` 对 tools 生成三段式指纹（before/search/after 拆分，`_split_tools_by_search` hooks_adapter.py:52-82），计算 8 位 `prefix_fp`（`_fp8`），扫描 cache_breakpoints 判断缓存分段，产出 cached_tokens/hit_ratio 指标。
**收益**：应用层可观测「工具列表变化导致缓存失效」的成本，优化工具暴露策略。
**代码事实**：hooks_adapter.py:42-82（指纹工具）、261（on_before_llm_call）。

---

## 6. 对外能力清单

### Facade API（4 个）

| Facade | 关键方法 | 说明 |
|--------|---------|------|
| `AuditLog` | `write_sync(event_type, *, agent_id, run_id, detail, ...)` → None，失败抛 `AuditWriteError`；`query_records(...)` | HC4 强一致，同步写+flush 原子 |
| `Tracer` | `start_span(name, span_type, *, run_id, parent_span_id, ...)` → Span；`end_span(span, *, status, attributes)`；`mark_span_error`；`query_trace(run_id)`；`build_trace_context(parent_span)` | span 树 + 跨 Agent 传播 |
| `Metrics` | 通用：`record_duration` / `increment_counter` / `set_gauge` / `record_tokens` / `flush`；命名：`inc_run_total` / `inc_step_total` / `inc_llm_call_total` / `inc_tool_execute_total` / `inc_error_total` / `inc_permission_check_total` / `inc_hitl_approval_total` / `observe_*_duration_ms` ×4 / `set_active_runs` | metrics.py:51-202 |
| `Logger` | `log(level, msg, **context)` / `debug/info/warn/error` | 结构化 JSON（logger.py:36-52）|

### 替换扩展点 Protocol（5 个，protocols.py）

| Protocol | 方法 | 用途 |
|----------|------|------|
| `AuditBackend` | `write` / `flush` / `query` | 审计存储（不可关闭）|
| `TracerBackend` | `export_span` / `query_spans` | span 存储 |
| `MetricsBackend` | `record_counter` / `record_histogram` / `record_gauge` | 指标存储 |
| `LoggerBackend` | `write_log` | 日志存储 |
| `Sanitizer` | `sanitize` | 写入前脱敏 |

### 关键契约

1. **所有权**：AuditLog 强一致（写失败=run 失败）；其余三支柱 Fail-Safe（失败=debug 留痕，绝不传播）。
2. **不可变**：AuditRecord / Span / ObservabilityContext 均为 frozen dataclass；ObservabilityConfig 亦 frozen。
3. **异常语义**：`AuditWriteError` 独立基类（不继承 ObservabilityError），防上层 `except ObservabilityError` 意外吞掉审计错误（exceptions.py:9-14）。
4. **共享-独立二分**：Facade 可多 Agent 共享（adapter 按 run_id 隔离）；session 数据物理分片（`_no_session` 兜底）。
5. **SDK 不计价**：只记 token 事实，金额归应用层（provider.py:82 注释）。

### 上下游模块

- **上游依赖**：`engine/hook/hooks.py`（AgentHooks 21 点）、`behavior/`（PermissionGuard 审计）、`tool/`、`skill/`、`identity/`（agent_id）、`observability/types.py`（generate_id）。
- **下游消费**：`builder.py`（装配入口）、`engine/loop.py`（audit_log + hooks 注入）、pandapal 应用层（看板聚合 / 会话列表）。

---

## 7. 关键代码与设计要点

1. **`AuditWriteError` 独立基类**（exceptions.py:9-14）——注释明说「与 ObservabilityError 不共享基类，防止上层意外吞掉」。审计错误必须「与众不同」地传播。
2. **Null 后端替代 None 检查**（provider.py:42-53）——关闭态不是「没有后端」而是「有后端但不做事」，调用方代码无需分支判断，天然空对象模式。
3. **surrogate 清洗防全链路崩溃**（audit.py:24-37）——上游编码错误混入 U+D800-DFFF 会导致 `json.dumps` / `open(utf-8)` 抛 UnicodeEncodeError；写入前统一 `surrogateescape→replace` 清洗，fallback 路径二次清洗（audit.py:174-177）。
4. **路径分量防逃逸**（markdown.py:41-50）——session_id 若含 `/` `\` `..` 会逃出分片目录；`_sanitize_component` 白名单过滤 + 拒绝纯点。
5. **`_ActiveSpan` 用 `__slots__` + noop span 降级**（tracer.py:251-285）——活跃 span 运行时状态与对外 Span 对象分离，`__slots__` 省内存；后端不可用时返回空 span_id 的 noop，调用方 `if span.span_id` 即可判断，无需异常。
6. **BUG 修复文化写进注释**（hooks_adapter.py:131/169/178/257）——每个修复点标注「Bug N Fix: 根因 + 为什么这样改」，后续维护者能读懂事故史。

---

## 8. 数据流

### 链路 A：Loop 观测（可降级通道）

```
AgentLoop Phase 2→6（消息构建/LLM 调用/工具执行/决策）
   │ 触发 on_before_llm_call / on_after_tool_call 等 hook
   ▼
ObservabilityHooksAdapter（按 run_id 分桶缓冲）
   ├─▶ Logger._format_record → 结构化 dict（log_id/timestamp/level/run_id/session_id/step_n）
   ├─▶ Tracer.start_span/end_span → Span（脱敏属性 → duration_ms 计算 → backend.export_span）
   └─▶ Metrics.inc_*/observe_* → backend.record_counter/histogram/gauge
   ▼
Console（stderr）/ InMemory（内存）/ Markdown（session 分片文件）/ SQLite（四表）
```

### 链路 B：审计（强一致通道）

```
engine/behavior/tool 内 AuditLog.write_sync(event_type, agent_id, run_id, detail, ...)
   → surrogate 清洗 → AuditRecord 构建（severity 查 _DEFAULT_SEVERITY 表，audit.py:39-64）
   → Lock 内 backend.write + backend.flush（原子）
   → 失败 → stderr JSON fallback → raise AuditWriteError（传播到 Loop，O3 转为 AgentResult 错误）
```

### 链路 C：查询（写后读）

```
用户 query_records(run_id) / query_trace(run_id)
   → Facade 委托 backend.query（InMemory 内存过滤 / SQLite WHERE）
   → 返回 frozen 记录列表（时间倒序，limit 截断）
```

**多路径对比**：链路 A（观测）与链路 B（审计）的差异即整个模块的核心设计张力——A 可以丢、可以降级、可以采样（TraceLevel）；B 一条都不能丢，丢了就要让 run 失败。写代码时「该走哪条通道」由业务重要性决定：高风险操作（PERMISSION_DENIED/HITL）走 B，性能数据走 A。

---

## 9. 架构问题与风险

| 级别 | 位置 | 问题 | 建议 |
|------|------|------|------|
| P0 | — | **无**（未发现破坏性/数据丢失级缺陷）| — |
| P1 | — | **无**（审计强一致 + 观测全 Fail-Safe 已闭环；未发现静默丢失关键数据的路径）| — |
| P2 | `observability/__init__.py:28-37` | 对外导出面漏 `InMemoryLoggerBackend`：`backend/__init__.py:16-21` 已导出，但顶层 `from pandaren.observability import InMemoryLoggerBackend` 会 ImportError（其余 15 个后端类均可达）| 在 `__init__.py` 的 InMemory 组补上 `InMemoryLoggerBackend` |
| P2 | `logger.py:60-61` | `_write` 的 `except Exception: pass` 纯静默，违反 §九「降级必留痕」；同层 metrics.py/tracer.py 都用 `logger.debug(exc_info=True)` 留痕，唯独 logger 自身静默 | 改为 `logging.getLogger(__name__).debug("logger backend write failed", exc_info=True)` |
| P3 | `backend/in_memory.py:49-71` | `InMemoryTracerBackend` 无锁（同文件其余 3 个后端均有 `threading.Lock`），并发 export_span 下 append/切片非严格原子 | 测试用途风险低；若要严格一致，补一把锁 |
| P3 | `audit.py:147-149` | 每次 `write_sync` 都 `flush`——SQLite 下每条审计一次落盘，高频 tool 执行场景是同步瓶颈（HC4 强一致取舍）| 如遇性能瓶颈可评估「批量 + 崩溃时补记」策略，但需重新论证 HC4 一致性 |

---

## 10. 课程案例素材提炼

| 教学点 | 代码事实 |
|--------|---------|
| 同一层内的两种容错哲学：哪些必须失败、哪些可以降级 | audit.py:146-155（传播）vs tracer.py:149-150 / metrics / logger（debug 留痕）|
| 并发缓冲隔离：共享实例 + run_id 分桶 | hooks_adapter.py:178-200（兜底关闭）+ 87aea2c（事故修复）|
| 空对象模式：Null 后端替代 None 分支 | provider.py:42-53 |
| 显式四态：`_UNSET` 哨兵消除「默认开了吗」歧义 | builder.py:92 + provider.py:91-177 |
| 错误类型隔离：独立基类防被吞 | exceptions.py:9-14 |
| 写前清洗：surrogate 清洗防存储层崩溃 | audit.py:24-37 |

---

## 11. 验证信息与沿革

**测试覆盖**：5 个测试文件（共约 154KB）+ 1 份设计文档（sqlite 读防御）。pytest testpaths 含 `pandaren`，`pandaren/observability/tests/` 全覆盖：facade 功能、Mock 引擎全链路、数据隔离、SQLite 后端、SQLite 读防御。

**变更历史**（`git log -- pandaren/observability`，代码有变则本节可能过期）：
- `87aea2c` fix(observability): 按 run_id 隔离 span 缓冲，修复多会话并发串扰 + sqlite 读防御
- `c7d5e9f` Initial commit

**与上下篇印证**：
- 上游：`pandaren/hook/hooks.py`（21 个生命周期点，adapter 实现其中 19 个；skill_activated/cleared 由 SkillManager 处理，见 PANDAPAL.md）
- 下游：`pandaren/builder.py:891`（`_build_observability_and_hooks` 前移到工具注册之前，确保注册事件可被审计）
- 平行：`pandapal/events/normalized.py`（应用层把 StreamEvent → NormalizedEvent 供前端，与 SDK 观测层互补：SDK 管存储，应用层管推送）

---

### 自检（红线落地）

- ☑ 11 节全部写出，无缺失
- ☑ P0/P1 状态明确声明（均为「无」）
- ☑ 方案总览含场景穷举表（已有/缺失均有依据）+ 总体方案思路
- ☑ 模块整体框架 ASCII 图已画（分层 + 数据流方向 + 关键组件，基于真实目录）
- ☑ 核心机制 7 条，每条含痛点 → 机制 → 收益 → 代码事实
- ☑ 产品视角 6 项齐全（旅程 / 量化反面案例 / 地图 / 边界 / 失败体验 / 成熟度）
- ☑ 抽查锚点：`audit.py:98-103`（backend None 拒绝）、`tracer.py:205-223`（_should_record）、`hooks_adapter.py:178-200`（兜底关闭）、`provider.py:164-171`（False 降级 WARN）均与源码一致
