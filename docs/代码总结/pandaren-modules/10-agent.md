# pandaren/agent — 顶层 Agent 封装（运行时门面 + 配置快照）

> 模块：pandaren/agent | 生成：2026-08-19 @ git `6580f7b` | 锚点以生成时点代码为准

---

## 1. 模块定位与职责（存在的意义）

**为什么存在**：让「Agent」成为用户手里最薄的一个门面——`Agent.run()` 永不抛异常（O3）、`agent_id` 随手可得、生命周期可管理；同时让多 Session 并发场景下 **一份配置、多份隔离实例** 成为可能（AgentBlueprint 共享/独立二分）。

**不建它会怎样**：
- 用户得直接跟 AgentLoop 打交道，Loop 的构造参数（15+ 个组件）全部暴露，构建 3 行变 30 行；
- 每个 Session 各建一套 LLM client / tool registry / audit log——连接池爆炸、审计通道分裂、工具注册状态无法共享；
- Agent 实例被多 Session 并发共享 → STM/session_meta 串话（多 Session 并发改造的动机，见 blueprint.py:1-39 设计文档引用）。

**角色分工图**：

```
┌──────────────────────────────────────────────────────────────────┐
│  AgentBuilder（配置收集器，builder.py）                            │
│    .identity() .llm() .tools() ... 16 个 fluent 方法              │
│        │ build_blueprint()（builder.py:798）                      │
│        ▼                                                          │
│  AgentBlueprint（配置快照 + 共享组件容器，frozen dataclass）         │
│    ├─ 共享字段：llm_client / tool_registry / audit_log / ...      │
│    ├─ 独立工厂：memory_factory() / hooks_template.clone()         │
│    └─ materialize() → 新 Agent（blueprint.py:154）                │
│        │                                                          │
│        ▼                                                          │
│  Agent（运行时门面，agent.py）──持有──► AgentLoop（engine/loop.py） │
│    run() / run_stream() 纯转发，零代码共享                          │
│    aclose() 幂等，不关共享 client（所有权边界）                     │
│    cancel() / rebind_system_prompt()                              │
└──────────────────────────────────────────────────────────────────┘
        ▲ 消费方
   pandapal/scheduler/agent_pool.py:503  ── blueprint.materialize()（每 session 一份）
   pandapal/subsystem_container.py:75    ── 只传 AgentBlueprint，不传单 Agent 实例
```

**覆盖文件清单**（源码 2 个 + 测试 3 个）：

| 文件 | 大小 | 职责 |
|------|------|------|
| `agent.py` | 9.0KB / 212 行 | Agent 运行时门面 + AgentStatus 枚举 |
| `blueprint.py` | 9.9KB / 233 行 | AgentBlueprint frozen dataclass（快照 + 共享容器 + materialize 工厂）|
| `tests/test_agent.py` | 40.7KB | ★ 混测 sub_agent registry/loader + Agent class + builder 集成 |
| `tests/test_agent_mock.py` | 52.4KB | ★ Mock 引擎测试（Agent 行为 + SubAgentRegistry + loader）|
| `tests/test_agent_cache.py` | 24.5KB | ⚠️ 整文件 `pytest.skip`：缓存效果验证脚本，依赖已移除的 memory.backend（API 漂移债）|

---

## 2. 方案总览（产品视角）

> 非技术读者可只读本节。

### 2a. 在什么场景下解决什么问题（场景穷举）

| 场景 | 已有/缺失 | 该场景下的问题 |
|------|----------|---------------|
| 单 Agent 跑一次任务（demo/测试） | 已有 | `.build()` 一步到位，Agent 薄门面转发 Loop |
| 桌面端多 Session 并发（每会话一个 Agent） | 已有 | blueprint.materialize() 每 session 一份隔离实例，共享组件复用 |
| 共享 LLM client 的生命周期管理 | 已有 | 谁创建谁关闭：Agent 借用不关，Blueprint 是 owner-of-record 唯一关闭点 |
| 运行时切换人格/领域（保留对话历史） | 已有 | rebind_system_prompt() 转发 Memory.set_system_prompt |
| 预算前置拦截（run 前查额度） | 已有 | Agent.provider 透传底层 LLM client 的 provider（agent.py:70-78）|
| model_id 端到端落库（暂停/恢复不回落） | 已有 | Agent.model_name 透传具体模型名（agent.py:80-90）|
| 真实 LLM 的 prefix cache 命中率验证 | **缺失** | test_agent_cache.py 整体 skip，缓存效果无自动化验证（代码注释自认漂移债）|
| 自定义 Agent 注册表健康管理 | 已有 | AgentStatus 三态枚举（HEALTHY/UNHEALTHY/DRAINING），供 SubAgentRegistry 使用 |

### 2b. 总体方案思路（策略）

1. **极薄门面**：Agent 不实现任何业务逻辑，`run()`/`run_stream()` 逐参转发 AgentLoop——API 稳定面与实现面分离，Loop 怎么改，Agent 签名不动。
2. **Blueprint 中间层**：Builder（一次性配置）→ Blueprint（长期快照，跨 App 生命周期）→ Agent（按需实例）。三层各司其职，多 Session 只需「配置一次、实例化 N 次」。
3. **共享/独立二分契约**：无状态 / 只读 / 按外部键索引的组件共享；带会话状态的组件（Memory、Hooks）用工厂每实例产出。新增字段必须先登记契约表（blueprint.py:34-38 注释强制流程）。
4. **所有权显式化**：client 谁创建谁关闭（Agent 不关共享 client）；Blueprint 是共享引用的 owner-of-record，唯一合法关闭点。
5. **Fail-Safe 快照校验**：必填字段 None → `__post_init__` 立即 raise，不接受静默默认（E4）。

---

## 3. 产品视角

### 3a. 使用场景与用户旅程

**谁**：SDK 用户（build().run() 一把梭）、应用层开发者（pandapal 的 run_local 构造 blueprint）、框架维护者（加组件字段）。
**典型旅程**：`AgentBuilder().identity(...).llm(client).tools([...]).build_blueprint()` 启动时构造一次 → 每 Session `blueprint.materialize()` 产出隔离 Agent → `agent.run_stream()` 消费事件 → 进程停机时 `await blueprint.aclose()` 关共享连接池。

### 3b. 量化价值与反面案例

- **反面案例 1（共享 client 误关）**：若 `Agent.aclose()` 顺手关掉 llm_client，驱逐/丢弃单个会话实例会连带关掉整个进程共享的 HTTP 连接池，其余所有 session 报「client has been closed」——一处清理、全线瘫痪。代码以注释形式把这条事故教训钉死在 aclose() 里（agent.py:176-180）。
- **反面案例 2（单实例多 Session 共享）**：改造前每个 session 共用一个 Agent → Memory 的 STM/session_meta 按 session 隔离失效，串话风险（blueprint.py:1-39 引用的多 Session 并发改造设计文档为据）。
- **量化收益**：1 个 blueprint → N 个互不干扰的 Agent 实例（`agent_a._loop._memory is not agent_b._loop._memory`，blueprint.py:86-87）；llm_client/tool_registry/audit_log 等共享组件零重建；审计仍走单一 AuditLog 通道（HC4）。

### 3c. 产品地图定位

能力域：pandaren SDK 顶层封装（agent/ 是 pandaren 对外的「脸」）。
上游：identity（agent_id/name）、engine.loop（AgentLoop）、llm.types（ModelSettings）。
下游：builder.py（装配）、pandapal（scheduler/agent_pool、subsystem_container、local/run_local）、sub_agent（AgentStatus 被 SubAgentRegistry 使用）。

### 3d. 能力边界与承诺

**能承诺（代码强制）**：
1. `run()` 永不抛异常，永远返回 AgentResult（O3，转发 AgentLoop 语义）。
2. Agent 实例间的 Memory/Hooks 物理隔离（materialize 工厂强制）。
3. 必填字段缺失立即失败（`__post_init__` raise，blueprint.py:117-152），无静默默认。
4. `aclose()` 幂等；共享 client 只由 Blueprint 关闭一次。

**明确不做**：
1. Agent 不持有/不创建 llm_client（借用方，关闭权归 Blueprint）。
2. 不做会话级状态管理本身（那是 Memory 的职责，Agent 只是门面）。
3. 不实现 Agent 注册表逻辑（AgentStatus 枚举只是给 SubAgentRegistry 用的数据契约）。

### 3e. 用户视角的失败体验

| 技术风险 | 用户看到 |
|---------|---------|
| blueprint 必填字段漏传 | 启动即 `ValueError: AgentBlueprint requires xxx`（fail-fast，不会运行到一半才崩）|
| memory_factory 传了非 callable | `TypeError: AgentBlueprint.memory_factory must be callable`（鸭子类型校验）|
| hooks_template 没有 clone() | `TypeError: ... must have clone() method`（防共享 hooks 状态串扰）|
| materialize 中途失败（memory_factory 抛异常） | 异常向上传播，消费方（Pool.acquire）负责释放 semaphore——不吞不兜（blueprint.py:164-167）|
| 底层 LLM client 不可达 | `agent.provider` / `agent.model_name` 返回 `""`（Fail-Safe，不阻断 run 启动，agent.py:75/87）|

### 3f. 成熟度与演进路线

当前状态：**稳定**（3 个 commit 触及，最近一次 `6580f7b` 是 skill/hook 重构的连带改动；多 Session 并发改造已完成，契约表在案）。
已知演进方向：真实 LLM cache 验证脚本重写（test_agent_cache.py 的漂移债）；agent/tests 混测 sub_agent 的归属整理。

---

## 4. 模块整体框架

```
AgentBuilder（builder.py）
  build()                = build_blueprint().materialize()   （builder.py:793-796）
  build_blueprint()      → AgentBlueprint                     （builder.py:798-877）
                                   │
                 ┌─────────────────┴──────────────────┐
                 ▼                                     ▼
        AgentBlueprint（blueprint.py）         Agent（agent.py）
        frozen dataclass                       属性：identity/agent_id/agent_name
        ├─ 共享 16 字段（传引用）                 provider/model_name（透传+Fail-Safe）
        ├─ 独立工厂 2 个（每实例产出）           方法：
        │    memory_factory → Memory                run() → AgentResult（O3）
        │    hooks_template.clone → Hooks           run_stream() → StreamEvent 生成器
        ├─ __post_init__：必填校验                 aclose()（幂等，不关共享 client）
        ├─ materialize() → 5 步产出 Agent          cancel()（协作式取消）
        └─ aclose() → 关共享 llm_client            rebind_system_prompt()（换人格）
                                                   __aenter__/__aexit__（async with）
```

**读图要点**：
1. **三层生命周期**：Builder 一次性（收集配置即使命完成）→ Blueprint 长期（贯穿 App）→ Agent 按需（materialize 即时产出，可随时丢弃）。
2. **两个工厂点**：Memory 和 Hooks 是「每实例」的，其它全是「共享引用」——这是多会话隔离的全部秘密。
3. **两个关闭点**：Agent.aclose()（自身，近空实现）与 Blueprint.aclose()（共享 client，唯一合法关闭点）——错用后果见 §3b 反面案例 1。

---

## 5. 核心机制详解

### 5.1 所有权边界：谁创建谁关闭（共享 client 不误关）

**痛点**：llm_client 由调用方注入、被多 Agent 共享。若每个 Agent 关闭时都关它，驱逐一个实例 = 全进程连接池瘫痪。
**机制**：`Agent.aclose()` 幂等但**不触碰 llm_client**（agent.py:171-186，注释明写「共享 client 的关闭权归其创建者/容器」）；`AgentBlueprint.aclose()` 是共享 client 的唯一合法关闭点，幂等 + best-effort（单个失败不阻断整体，blueprint.py:207-225）。
**收益**：实例级清理与容器级清理边界清晰，驱逐/重启单会话零连带伤害。
**代码事实**：agent.py:176-180（所有权注释）、blueprint.py:211-217（owner-of-record 声明）。

### 5.2 materialize 工厂：共享引用 + 独立产出

**痛点**：多 Session 既要复用昂贵组件（连接池/注册表/审计），又要隔离会话状态（Memory/Hooks）。
**机制**：`materialize()` 5 步（blueprint.py:154-205）：① `memory_factory()` 新 Memory → ② `hooks_template.clone()` 新 hooks → ③ AgentLoop（16 个引用：14 共享 + 2 独立）→ ④ Agent 包装 → ⑤ return。构造失败异常全传播（消费方 Pool.acquire 负责释放 semaphore）。
**收益**：`agent_a._loop._memory is not agent_b._loop._memory` ✓、`_llm_client is` ✓——共享与隔离同时成立。
**代码事实**：blueprint.py:172-197（5 步实现）、86-87（断言注释）。

### 5.3 共享/独立二分契约表（新增字段先登记）

**痛点**：新组件乱入共享字段 → 会话状态串扰（改一次炸一片）。
**机制**：blueprint.py:11-38 契约表注释——「无状态 / 只读 / 按外部键索引」至少满足一项才可共享，否则必须走工厂；新增字段流程 4 步（登记契约表 → 论证共享性 → 改代码 → Code Review 强制审查）。
**收益**：共享性决策有据可查、有流程把关，防「顺手加个共享字段」。
**代码事实**：blueprint.py:11-38。

### 5.4 frozen 快照 + __post_init__ Fail-Safe

**痛点**：配置快照被运行期意外修改 → 不可复现的 bug。
**机制**：`@dataclass(frozen=True)`（blueprint.py:70）构造后全字段不可变；`__post_init__` 对 12 个必填字段做 None 校验 + 2 个鸭子类型校验（memory_factory callable、hooks_template 有 clone()），缺失即 raise（blueprint.py:117-152）。
**收益**：坏配置在构造瞬间暴露，不拖到第一次 run 才炸。
**代码事实**：blueprint.py:70、117-152。

### 5.5 run/run_stream 零代码共享 + 双签名

**痛点**：run() 与 run_stream() 行为不一致 → 用户踩坑。
**机制**：两个方法各自完整转发 Loop 对应方法，**零代码共享**（agent.py:116-124 vs 155-167），但参数镜像：run_stream 额外支持 `plan_action`/`edited_plan_content`/`settings`（Plan Mode 交互参数），run() 不带。
**收益**：流式与阻塞语义各自独立演进，参数面显式区分。
**代码事实**：agent.py:116-124、155-167。

### 5.6 provider/model_name 透传（Fail-Safe 反射）

**痛点**：预算拦截和 model_id 落库需要「具体」的 provider/model 名，但 Agent 不直接持有 client。
**机制**：`getattr(self._loop, "_llm_client", None)` 反射取 Loop 私有字段，转发 client 的 `provider`/`model_name`（构造后不可变）；不可达返回 `""`（Fail-Safe 不阻断）。
**收益**：应用层 run 启动前可做预算前置拦截（BudgetLedger.is_exhausted），暂停/恢复时 model_id 有真实名字可落库，杜绝静默回落。
**代码事实**：agent.py:70-90（docstring 写明用途与 Fail-Safe 语义）。

### 5.7 脱敏 repr：不泄露 api_key

**痛点**：AgentBlueprint 的 repr 若打印全部字段，会把 llm_client 的 api_key 泄进日志。
**机制**：自定义 `__repr__` 只输出 `identity.agent_id`、工具数、stream 标志（blueprint.py:227-233）。
**收益**：日志/调试输出零密钥泄露。
**代码事实**：blueprint.py:227-233。

---

## 6. 对外能力清单

### 对外 API（3 个类 + 1 个枚举）

| 类型 | 成员 | 说明 |
|------|------|------|
| `Agent` | `run(task, *, session_id, resume_state=None, hitl_decision=None, interaction_response=None, metadata=None, skill_name=None)` → AgentResult | 阻塞执行，永不抛异常（O3）|
| `Agent` | `run_stream(task, *, session_id, ..., plan_action=None, edited_plan_content=None, settings=None)` → AsyncGenerator[StreamEvent] | 流式执行，与 run() 零代码共享 |
| `Agent` | `aclose()` / `cancel()` / `rebind_system_prompt(prompt)` / `__aenter__` / `__aexit__` | 生命周期 + 协作取消 + 运行时换人格 |
| `Agent` | 属性 `identity` / `agent_id` / `agent_name` / `provider` / `model_name` | 只读门面 |
| `AgentBlueprint` | `materialize()` → Agent | 每调用产出一个全新隔离实例 |
| `AgentBlueprint` | `aclose()` | 关闭共享 llm_client（owner-of-record）|
| `AgentStatus` | `HEALTHY` / `UNHEALTHY` / `DRAINING` | Agent 健康状态（供 SubAgentRegistry）|

### 关键契约

1. **共享/独立二分**：identity、llm_client、tool_registry、permission_guard、hitl_controller、harness_executor、audit_log、execution_limits、error_policy、system_prompt、llm_settings、skill_registry、agent_registry、step_guard、context_window_budget、stream 共享；memory、hooks 每实例独立（blueprint.py:11-38）。
2. **所有权**：client 谁创建谁关闭——Agent 只借不关，Blueprint 唯一合法关闭点。
3. **Fail-Safe**：必填缺失 raise；provider/model_name 不可达返回 `""`。
4. **构造期校验**：frozen + __post_init__，坏配置构造即炸。

### 上下游模块

- **上游依赖**：identity.models（Identity）、engine.loop（AgentLoop）、engine.models（AgentResult/RunState）、engine.stream（StreamEvent）、llm.types（ModelSettings）、behavior.*（PermissionGuard/HITLController/HarnessExecutor/ExecutionLimits/ErrorPolicy/StepGuard/ContextWindowBudget）、hook.hooks（CompositeAgentHooks）、memory.memory（Memory）、observability.audit（AuditLog）、tool.registry（ToolRegistry）。
- **下游消费**：builder.py:793-877（build/build_blueprint）、builder.py:1305-1307（子 Agent blueprint 化）、pandapal/scheduler/agent_pool.py:503（每 session materialize）、pandapal/subsystem_container.py:75（只传 blueprint）、pandapal/local/run_local.py:392（构造 blueprint）、pandaren/sub_agent/registry.py（AgentStatus）。

---

## 7. 关键代码与设计要点

1. **`build()` = `build_blueprint().materialize()`**（builder.py:793-796）——单 Agent 场景是「快照+实例化」的语法糖，两条路径天然同构，不会出现「build 出来的 Agent 和 materialize 出来的不一样」。
2. **`_loop._memory.set_system_prompt` 私有穿透**（agent.py:203）——rebind_system_prompt 直接摸 Loop 私有字段，无类型保护；但注释声明了「供应用层在同一 session Agent 上切换人格」，调用方负责 delta 判断保护 prompt cache。
3. **`__post_init__` 校验清单与契约表同源**（blueprint.py:117-152 vs 11-38）——校验代码与契约注释一一对应，改契约必改校验，双向锁。
4. **异步上下文管理器**（agent.py:205-209）——`async with agent:` 离开即 aclose，推荐用法写进模块 docstring。
5. **子 Agent 同型不同构**（agent.py:1-12 docstring）——主/子 Agent 运行时类型完全相同，区别仅在构建路径（Builder 直构 vs SubAgentBlueprint → AgentBuilder → Agent），一套运行时吃两种装配。

---

## 8. 数据流

### 链路 A：构建期（配置收集 → 快照 → 实例）

```
AgentBuilder（16 个 fluent 方法收集配置）
   │ build_blueprint()（builder.py:798）
   ▼
AgentBlueprint.__init__ + __post_init__（12 必填校验）
   │ 每 session 调 materialize()（pandapal/agent_pool.py:503）
   ▼
memory_factory() → 新 Memory ｜ hooks_template.clone() → 新 hooks
   ▼
AgentLoop（14 共享引用 + 2 独立引用）→ Agent 包装 → return
```

### 链路 B：运行期（pandapal 消费）

```
SessionAgentPool.acquire（semaphore 闸门）
   → blueprint.materialize()  # 每 session 全新实例（agent_pool.py:503）
   → AgentExecutor 调 agent.run_stream(task, session_id=..., ...)
   → Agent 逐参转发 AgentLoop.run_stream
   → StreamEvent 逐个 yield → pandapal 转 NormalizedEvent → 前端
```

### 链路 C：关闭期（两级清理）

```
Agent.aclose()          # 幂等；只标记自身 closed，不关共享 client
AgentBlueprint.aclose() # 幂等；关共享 llm_client 连接池（owner-of-record）
                       # 调用时机：进程停机且确认无 in-flight Agent 时调用一次
```

**变更点**（多 Session 并发改造前后）：改造前「一个 Agent 实例服务所有 session」（Memory 串话风险）；改造后「blueprint 共享 + materialize 隔离」（agent_pool.py:503 每次 acquire 新实例）。

---

## 9. 架构问题与风险

| 级别 | 位置 | 问题 | 建议 |
|------|------|------|------|
| P1 | `tests/test_agent_cache.py:40` | 整文件 `pytest.skip`：真实 LLM prefix cache 验证（cache_control 断点 / cached_tokens）**无自动化覆盖**，代码注释自认「依赖已移除的 pandaren.memory.backend（现为 backends/*），API 漂移债，待重写」| 按新 backends 接口重写该脚本；重写前 cache 行为只能靠手工脚本验证 |
| P2 | `tests/test_agent.py` / `test_agent_mock.py` | agent/tests 目录**混测 sub_agent**（SubAgentRegistry/loader/models 大量用例，两文件 60+ 测试函数中多数属于 sub_agent 域）——模块测试归属混乱，sub_agent 行为变更时此处静默红/绿难排查 | 把 sub_agent 用例迁到 sub_agent 自己的 tests/，agent/tests 只留 Agent/Blueprint 用例 |
| P3 | `agent.py:77/89` | `getattr(self._loop, "_llm_client", None)` 反射穿透 Loop 私有字段——Loop 字段改名即静默失效（Fail-Safe 兜底返回 ""，但 provider 预算拦截会变盲）| 给 AgentLoop 加公开只读属性 `llm_client`，替换反射 |
| P3 | `agent.py:203` | `rebind_system_prompt` 直接摸 `self._loop._memory`（私有字段无类型保护）| 同上，Loop 暴露公开方法 `set_system_prompt` 转发 |
| P3 | `blueprint.py:110-114` | `skill_registry` / `agent_registry` / `step_guard` / `context_window_budget` 类型标注为 `Any`（可选字段），鸭子类型靠消费方兜 | 收敛为 Protocol 或具体类型（低优先级，均只读）|

---

## 10. 课程案例素材提炼

| 教学点 | 代码事实 |
|--------|---------|
| 所有权边界：谁创建谁关闭，借用方绝不清理共享资源 | agent.py:171-186 + blueprint.py:207-225（共享 client 误关 = 全进程连接池瘫痪）|
| 三层生命周期拆分：收集器 / 快照 / 实例 | builder.py:793-877 + blueprint.py:70-205 |
| 共享 vs 独立的判据：无状态/只读/外部键索引 | blueprint.py:11-38 契约表 |
| 工厂方法模式：memory_factory + clone() 产出隔离实例 | blueprint.py:172-177 |
| frozen dataclass + __post_init__：坏配置构造即炸 | blueprint.py:70、117-152 |
| 薄门面模式：API 稳定面与实现面分离 | agent.py:116-167（run/run_stream 纯转发）|

---

## 11. 验证信息与沿革

**测试覆盖**：3 个测试文件。
- `test_agent_mock.py` 覆盖 Agent 自身行为：属性透传（test_agent_properties）、run/run_stream 委托 Loop（test_agent_run_delegates_to_loop / test_agent_run_stream_delegates_to_loop）、aclose 幂等（test_agent_aclose_idempotent）、**aclose 不关共享 client**（test_agent_aclose_no_llm_client，所有权边界的显式用例）、async context manager、resume_state 透传、aclose 异常兜底（test_mock_agent_aclose_exception_handled）。
- `test_agent.py`：test_agent_class（Agent 行为）+ test_integration（builder 完整链路）。
- `test_agent_cache.py`：**整体 skip**（漂移债，见 §9 P1）。

**变更历史**（`git log -- pandaren/agent/`，代码有变则本节可能过期）：
- `c7d5e9f` Initial commit
- `f879aff` feat: 子 Agent 支持显式 model/llm_settings 配置 + registry 工厂侧并发隔离（触及 agent/，主要改 sub_agent）
- `6580f7b` refactor(skill/hook): 删除死代码、修复 Action 静默降级、移除 hooks 兼容别名（触及 agent/）

**与上下篇印证**：
- 上游：`pandaren/engine/loop.py`（AgentLoop，Agent 的转发目标）、`pandaren/hook/hooks.py`（CompositeAgentHooks.clone，materialize 的隔离来源）
- 下游：`pandaren/builder.py:793-877`（build/build_blueprint 装配）、`pandapal/scheduler/agent_pool.py:503`（每 session materialize，会话并发池的实例来源）
- 平行：`pandaren/sub_agent/`（AgentStatus 消费方 + tests 混居）、`docs/design/multi-session-concurrency-reform.md`（Blueprint 设计的源头文档，本地保留）

---

### 自检（红线落地）

- ☑ 11 节全部写出，无缺失
- ☑ P0/P1 状态明确声明（P0 无；P1：test_agent_cache.py 整体 skip 的自动化覆盖缺口）
- ☑ 方案总览含场景穷举表（已有/缺失均有依据）+ 总体方案思路
- ☑ 模块整体框架 ASCII 图已画（三层生命周期 + 两个工厂点 + 两个关闭点，基于真实代码）
- ☑ 核心机制 7 条，每条含痛点 → 机制 → 收益 → 代码事实
- ☑ 抽查锚点：`agent.py:176-180`（所有权注释）、`blueprint.py:154-205`（materialize 5 步）、`blueprint.py:117-152`（__post_init__ 校验）、`agent.py:70-90`（provider/model_name 透传）均与源码一致；`test_agent_cache.py:40`（pytest.skip）已实测确认
