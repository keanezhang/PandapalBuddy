# AgentBlueprint 测试设计文档

- 被测对象：`pandaren/agent/blueprint.py` — `@dataclass(frozen=True) class AgentBlueprint`
- 测试栈：pytest + pytest-asyncio（aclose 为 async；**此处为推断，请确认**项目是否已有 pytest-asyncio / anyio）
- 设计依据：白盒源码（blueprint.py 全 233 行 + AgentLoop.__init__ + CompositeAgentHooks.clone/add + ToolRegistry.list_tools）
- 设计文档落点：项目根 `tests/design/`（**推断**：项目当前无 tests/ 目录，按 pytest 惯例建在根下；若用户另有约定请调整）
- 覆盖准则：分支覆盖（Branch）为默认目标，全部异常分支 + happy 分支均走到

---

## 1. 白盒分析摘要（代码事实）

| 事实 | 位置 |
|------|------|
| 12 个必填字段：identity / llm_client / tool_registry / permission_guard / hitl_controller / harness_executor / audit_log / execution_limits / error_policy / system_prompt / memory_factory / hooks_template；任一 None → `ValueError(f"AgentBlueprint requires {name} (got None)")` | blueprint.py:124-140 |
| memory_factory 非 callable → `TypeError("AgentBlueprint.memory_factory must be callable, got {type}")` | blueprint.py:142-146 |
| hooks_template 无 `clone` 属性（鸭子类型，只查 hasattr）→ `TypeError("...must have clone() method (use CompositeAgentHooks)")` | blueprint.py:148-152 |
| 可选字段默认：llm_settings / skill_registry / agent_registry / step_guard / context_window_budget = None；stream = True | blueprint.py:110-115 |
| materialize()：局部 import Agent → `memory_factory()` → `hooks_template.clone()` → `AgentLoop(17 个 keyword 参数，message_builder/output_parser 走默认)` → `Agent(identity, loop)` → return；无任何 try/except，异常天然上抛 | blueprint.py:169-205 |
| AgentLoop 的 17 个参数全为 keyword-only，构造仅赋值（无 I/O） | loop.py:85-120 |
| aclose()：`getattr(llm_client, 'aclose', None)` 为 None → return；异常 → `logger.warning`（不 re-raise）；**无内部 _closed 状态，幂等完全依赖底层 client.aclose() 可重复调用** | blueprint.py:219-225 |
| __repr__：`AgentBlueprint(identity={agent_id!r}, tools={len(list_tools())}, stream={stream})` — 不含 llm_client，故天然不含 api_key | blueprint.py:227-233 |
| CompositeAgentHooks.clone() 为浅拷贝：新实例 + `_hooks = list(self._hooks)`（列表独立、元素共享）；`add(hook)` 是公开注册方法 | hooks.py:292, 317-330 |

---

## 2. 不变式清单

| # | 不变式 | 来源 |
|---|--------|------|
| inv-1 | 构造后字段冻结：任何字段 setattr（含默认字段）→ FrozenInstanceError | frozen=True |
| inv-2 | Fail-Safe：12 个必填字段任一为 None → ValueError 且消息含字段名 | __post_init__ |
| inv-3 | 类型守卫：memory_factory 必须 callable、hooks_template 必须有 clone()，否则 TypeError | __post_init__ |
| inv-4 | materialize 共享二分：14 个共享组件跨调用为同一引用（is） | 契约 §3.3 |
| inv-5 | materialize 独立二分：memory / hooks 每次调用为全新实例（is not），且分别为 memory_factory() 与 clone() 的返回值 | 契约 §3.3 |
| inv-6 | 异常不吞：memory_factory() / clone() 抛异常时 materialize 原样上抛，且后续步骤不执行 | materialize 无 try/except |
| inv-7 | 执行顺序：memory_factory() 恰在 clone() 之前，两者各恰好调用 1 次 | materialize 步骤 1→2 |
| inv-8 | aclose 契约：client 无 aclose → 静默返回；client.aclose 抛异常 → warning 不阻断；可重复调用（幂等委托底层） | aclose 实现 |
| inv-9 | repr 脱敏：只含 identity.agent_id / tools 数量 / stream，绝不含 llm_client 任何属性（api_key） | __repr__ |
| inv-10 | 可选字段默认：5 个可选依赖默认 None 被透传，stream 默认 True | 字段默认值 |

## 3. 风险清单（按优先级排序）

| # | 风险 | 严重度 | 可能性 | 优先级 |
|---|------|:--:|:--:|:--:|
| Risk-1 | **跨 session 数据污染**：memory/hooks 未独立 → 会话 A 数据泄漏到会话 B | 高（数据损坏/隐私） | 中（每次 materialize 都走） | **P0** |
| Risk-2 | **Fail-Safe 失效**：必填字段缺失静默通过 → 构造出残缺 blueprint，运行时属性错误 | 高（运行时崩溃） | 中 | **P0** |
| Risk-3 | aclose 抛异常阻断停机 / 非幂等导致重复关闭崩溃 | 中 | 中 | **P1** |
| Risk-4 | repr 泄漏 api_key（脱敏失败） | 高（凭证泄漏） | 低（仅调试/日志触发） | **P1** |
| Risk-5 | materialize 吞异常：factory 失败被吞 → 调用方（Pool.acquire）semaphore 不释放 → 资源死锁 | 高 | 低（异常路径，回归风险） | **P1** |
| Risk-6 | frozen 失效：字段可篡改 → 共享组件被替换 | 中 | 低 | P2 |
| Risk-7 | 可选字段透传错误：step_guard / context_window_budget / llm_settings 漏传或传错引用 | 中 | 低 | P2 |
| Risk-8 | clone 浅拷贝边界误解：hooks 元素仍共享，带跨会话 buffer 的元素会造成污染（本期声明 YAGNI） | 中 | 低 | P2 |

---

## 4. Mock / Fake 策略

**零 mock 库，全内存 Fake。** 被测边界是「引用透传 + 调用编排」，Fake 精确可控且无需 mock 框架；真实 AgentLoop/Agent 构造仅赋值无 I/O，可放心使用。

| 依赖 | 决策 | 理由 |
|------|------|------|
| llm_client（materialize 路径） | Fake 哨兵（无方法） | 只透传引用，blueprint 不调用其方法 |
| llm_client（aclose 路径） | Fake 记录型：`aclose()` 计数 + 可配置抛异常 / 无 aclose | 需验证副作用与故障行为 |
| tool_registry | Fake：`list_tools()` 返回预置列表 | repr 依赖；materialize 只透传 |
| identity / 其余共享组件 | Fake 哨兵（各字段名唯一标识） | 只透传引用，断言 is 关系 |
| memory_factory | 计数函数 / 抛异常函数 / 返回预置 FakeMemory | 验证顺序、次数、传播 |
| hooks_template | 真实 `CompositeAgentHooks`（+ 哨兵子 hook） | 验证真实 clone 浅拷贝边界 |
| AgentLoop / Agent | **真实类**（局部 import 硬编码，无注入点） | 构造零 I/O；备选：monkeypatch `blueprint` 模块内符号隔离（本期不需要） |
| logger.warning | pytest `caplog`（内置 fixture，非 mock 库） | 副作用验证 |

---

## 5. Oracle 策略

| 断言类型 | 适用用例 | 依据 |
|----------|---------|------|
| golden value（错误消息/默认值/repr 全串） | U1-U5, U17 | 消息格式与 repr 模板代码白纸黑字，可独立推导，非自指 |
| is / is not 引用断言 | U6-U8, U12, U18 | 共享/独立二分本义 |
| 副作用计数 + 顺序 log | U9, U14, U16 | Fake 记录调用次数与顺序 |
| 异常类型 + 消息 match | U10, U11, U16 | factory/clone/client 故障注入的预期行为 |

---

## 6. 测试基础设施（Fake 设计，供 test-coder 落为 conftest/helper）

- `FakeIdentity(agent_id: str)` — 只需 agent_id 字段
- `SentinelLLMClient` — 无任何方法（materialize 透传用）
- `FakeCloseLLMClient` — 字段 `aclose_calls: int`、`fail_on_close: Exception | None`；`aclose()` 计数 + 可选抛异常
- `FakeToolRegistry(tools: list)` — `list_tools()` 返回 `tools`；`len` 供 repr 断言
- `FakeMemory(name: str)` — 哨兵，仅用于 is 断言
- `build_valid_params(...)` helper — 产出 12 必填齐全的合法参数字典，供缺字段用例逐一置 None
- 共享组件哨兵：每个组件一个独立 `object()`，命名如 `SENTINEL_TOOL_REGISTRY`（确保 is 断言有区分度）
- aclose 用例用 `pytest.mark.asyncio`（推断框架，见文档头）

---

## 7. 用例 × 风险/不变式覆盖矩阵

| 用例 | inv | Risk | 层级 |
|------|-----|------|------|
| U1 必填字段缺失（12 参数化） | inv-2 | Risk-2 [P0] | unit |
| U2 memory_factory 非 callable | inv-3 | Risk-2 [P0] | unit |
| U3 hooks_template 无 clone | inv-3 | Risk-2 [P0] | unit |
| U4 frozen 不可变 | inv-1 | Risk-6 [P2] | unit |
| U5 可选字段默认值 | inv-10 | Risk-7 [P2] | unit |
| U6 materialize 结构装配 | inv-5,6 | Risk-1 [P0] | component(fake) |
| U7 两次 materialize 独立 | inv-5 | Risk-1 [P0] | component(fake) |
| U8 两次 materialize 共享（14 组件） | inv-4 | Risk-1 [P0] | component(fake) |
| U9 调用顺序与次数 | inv-7 | Risk-5 [P1] | component(fake) |
| U10 memory_factory 异常传播 | inv-6 | Risk-5 [P1] | component(fake) |
| U11 clone 异常传播 | inv-6 | Risk-5 [P1] | component(fake) |
| U12 可选字段非 None 透传 | inv-4 | Risk-7 [P2] | component(fake) |
| U13 clone 浅拷贝边界 | inv-5 | Risk-8 [P2] | component(fake) |
| U14 aclose 正常调用 | inv-8 | Risk-3 [P1] | component(fake) |
| U15 aclose 无方法静默 | inv-8 | Risk-3 [P1] | component(fake) |
| U16 aclose 异常 best-effort + 幂等 | inv-8 | Risk-3 [P1] | component(fake) |
| U17 repr 脱敏 | inv-9 | Risk-4 [P1] | unit |
| U18 并发冒烟（可选） | inv-4,5 | Risk-1 延伸 [P3] | component(fake) |

层级声明：**无 integration / e2e**。理由：本组件不持有真实外部 I/O 边界——materialize 构造为纯内存装配，aclose 的真实连接池关闭属 llm_client 自身测试域，不属于 blueprint 职责；真实依赖测试需在 llm_client 层另行设计。

---

## 8. 用例展开

### U1：12 个必填字段任一为 None → ValueError（参数化 ×12）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 Fail-Safe + Risk-2 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: `if value is None` 真分支（12 字段逐一命中） |
| Oracle | golden value（消息模板代码白纸黑字） |
| Mock | 否 — 构造期纯逻辑 |

**等价类划分**：必填字段集合 {12 字段} → 代表值 = 每个字段各置 None 一次（`pytest.mark.parametrize` 覆盖全部，非抽样）；其余 11 字段保持合法值

**Given**：
- `build_valid_params()` 产出合法参数字典；将 `field_name` 置 `None`

**When**：
- `AgentBlueprint(**params)`

**Then**：
- 抛 `ValueError`，消息精确为 `"AgentBlueprint requires {field_name} (got None)"`
- 无副作用（构造失败，无实例产生）

### U2：memory_factory 非 callable → TypeError

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 + Risk-2 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: `if not callable(self.memory_factory)` 真分支 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：memory_factory 类型 → 无效类代表值 = `42`（int）；合法类（函数/lambda/带 `__call__` 实例）由 U6 happy 路径代表

**Given**：
- 合法参数，`memory_factory=42`

**When**：
- `AgentBlueprint(**params)`

**Then**：
- 抛 `TypeError`，消息含 `"memory_factory must be callable, got int"`
- 无副作用

### U3：hooks_template 无 clone() → TypeError

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 + Risk-2 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: `if not hasattr(self.hooks_template, "clone")` 真分支 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：hooks_template 形态 → 无效类代表值 = `object()`（无 clone）；合法类 = 真实 `CompositeAgentHooks`（U6 起使用）

**Given**：
- 合法参数，`hooks_template=object()`

**When**：
- `AgentBlueprint(**params)`

**Then**：
- 抛 `TypeError`，消息含 `"hooks_template must have clone() method (use CompositeAgentHooks)"`
- 无副作用

### U4：frozen 不可变 — 构造后字段赋值抛 FrozenInstanceError

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 + Risk-6 [P2] |
| 测试层级 | unit |
| 覆盖准则 | N/A（无分支，验证 dataclass 冻结机制） |
| Oracle | golden value（FrozenInstanceError 为 dataclasses 规范行为） |
| Mock | 否 |

**Given**：
- 合法构造 `blueprint = AgentBlueprint(**build_valid_params())`

**When**：
- `setattr(blueprint, "identity", FakeIdentity("hacked"))`

**Then**：
- 抛 `dataclasses.FrozenInstanceError`（FrozenInstanceError 是 AttributeError 子类，断言用具体类型防误匹配）
- 追加断言（同一用例内第二个动作）：`setattr(blueprint, "stream", False)` 同样抛 FrozenInstanceError（默认字段同样冻结）
- 副作用：blueprint 的 `identity` 仍是原引用（对象未被篡改）

### U5：可选字段默认值

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-10 + Risk-7 [P2] |
| 测试层级 | unit |
| 覆盖准则 | N/A |
| Oracle | golden value（默认值代码直读） |
| Mock | 否 |

**等价类划分**：可选字段取值 → 代表值 = 「完全不传」与「显式传 None」（两者应等价）

**Given**：
- 只传 12 个必填字段

**When**：
- `AgentBlueprint(**build_valid_params())`

**Then**：
- `blueprint.llm_settings is None`、`skill_registry is None`、`agent_registry is None`、`step_guard is None`、`context_window_budget is None`
- `blueprint.stream is True`
- 无副作用

### U6：materialize 结构装配 — 返回 Agent 的 memory/hooks 来自工厂与 clone

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 + inv-6 + Risk-1 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 语句覆盖：materialize 5 步全路径 |
| Oracle | is / is not 引用断言 |
| Mock | 否 — Fake 依赖（memory_factory 返回预置 FakeMemory、真实 CompositeAgentHooks） |

**等价类划分**：materialize 输入（工厂返回值形态）→ 代表值 = 预置 `FakeMemory("M1")` + 含 1 个子 hook 的 `CompositeAgentHooks`

**Given**：
- `memory_factory` 返回固定 `FakeMemory("M1")`
- `hooks_template = CompositeAgentHooks()` 且 `add(hookA)`
- 其余依赖用哨兵

**When**：
- `agent = blueprint.materialize()`

**Then**：
- `agent._loop._memory is M1`（factory 返回值被注入 loop）
- `agent._loop._hooks is not blueprint.hooks_template`（clone 出独立实例）
- `agent._identity is blueprint.identity`、`agent._loop._llm_client is blueprint.llm_client`（共享透传）
- 返回类型：`isinstance(agent, Agent)`（局部 import 后构造成功）
- 副作用：memory_factory 调用 1 次、clone 调用 1 次

### U7：两次 materialize — memory / hooks 彼此独立 [P0]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 + Risk-1 [P0]（跨 session 数据污染最高优先级） |
| 测试层级 | component(fake) |
| 覆盖准则 | N/A（不变式验证） |
| Oracle | is not 引用断言 + 副作用计数 |
| Mock | 否 — Fake 依赖 |

**Given**：
- 同一 blueprint（memory_factory 每次返回**新** `FakeMemory`，如按调用序号命名）

**When**：
- `a1 = blueprint.materialize()`；`a2 = blueprint.materialize()`

**Then**：
- `a1._loop._memory is not a2._loop._memory`（会话隔离核心断言）
- `a1._loop._hooks is not a2._loop._hooks`
- `a1 is not a2`（每次全新 Agent 包装）
- 副作用：memory_factory 共调用 2 次（每次 materialize 独立产出 memory）

### U8：两次 materialize — 14 个共享组件同一引用 [P0]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 + Risk-1 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | N/A（不变式验证） |
| Oracle | is 引用断言（成对遍历） |
| Mock | 否 — 全部哨兵，且**可选字段也填非 None 哨兵**（保证 is 断言有区分度，避免「None is None」假阳性） |

**Given**：
- blueprint 全字段填充：12 必填 + `llm_settings` / `skill_registry` / `agent_registry` / `step_guard` / `context_window_budget` 各填独立哨兵对象（如 `SENTINEL_STEP_GUARD`）

**When**：
- `a1 = blueprint.materialize()`；`a2 = blueprint.materialize()`

**Then**（对下表逐项断言 `a1._loop.<attr> is blueprint.<field>` 且 `a2._loop.<attr> is a1._loop.<attr>`）：

| blueprint 字段 | loop 属性 |
|----------------|-----------|
| identity | `_identity` |
| llm_client | `_llm_client` |
| llm_settings | `_llm_settings` |
| tool_registry | `_tool_registry` |
| harness_executor | `_harness_executor` |
| permission_guard | `_permission_guard` |
| hitl_controller | `_hitl_controller` |
| execution_limits | `_limits` |
| error_policy | `_error_policy` |
| step_guard | `_step_guard` |
| context_window_budget | `_context_window_budget` |
| audit_log | `_audit_log` |
| skill_registry | `_skill_registry` |
| agent_registry | `_agent_registry` |

- 副作用：无（仅引用关系）

### U9：materialize 执行顺序 — memory_factory 恰在 clone 之前、各恰 1 次

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 + Risk-5 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | N/A（顺序契约） |
| Oracle | 副作用顺序 log（Fake 记录调用时间戳/序号） |
| Mock | 否 — 计数包装 |

**Given**：
- `memory_factory` 包装为计数函数：每次调用 append `"memory"` 到共享 `call_log` 并返回新 FakeMemory
- `hooks_template.clone` 包装为计数：append `"clone"` 到同一 `call_log`（可用子类覆写 clone 或直接包装）

**When**：
- `blueprint.materialize()`

**Then**：
- `call_log == ["memory", "clone"]`（顺序：先 memory 后 clone）
- memory_factory 计数 1、clone 计数 1
- 副作用：无残留（两次调用各自独立）

### U10：memory_factory 抛异常 → 原样传播，clone 不执行 [P1]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 + Risk-5 [P1]（调用方依赖此异常释放 semaphore） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: materialize Step1 异常路径 |
| Oracle | golden（异常类型 + 消息原样） |
| Mock | 否 — 故障注入 Fake |
| 故障注入 | 故障类型：`RuntimeError("factory boom")`；注入点：`memory_factory()`；预期：异常向上传播，`hooks_template.clone()` **未被调用** |

**Given**：
- `memory_factory` 为抛 `RuntimeError("factory boom")` 的函数
- `hooks_template` 的 clone 计数可查

**When**：
- `blueprint.materialize()`

**Then**：
- 抛 `RuntimeError`，消息 `"factory boom"`（不包装、不吞）
- 副作用：clone 计数为 0（Step2 未执行，后续步骤短路）

### U11：hooks_template.clone() 抛异常 → 原样传播 [P1]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 + Risk-5 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: materialize Step2 异常路径 |
| Oracle | golden |
| Mock | 否 — 故障注入 Fake |
| 故障注入 | 故障类型：`RuntimeError("clone boom")`；注入点：`hooks_template.clone()`；预期：异常向上传播，AgentLoop 构造不执行 |

**Given**：
- memory_factory 正常（返回 FakeMemory）
- `hooks_template` 的 clone 抛 `RuntimeError("clone boom")`（如子类覆写或包装对象）

**When**：
- `blueprint.materialize()`

**Then**：
- 抛 `RuntimeError`，消息 `"clone boom"`
- 副作用：memory_factory 已调用 1 次（Step1 完成、Step2 失败，该 memory 成为孤儿——**无泄漏回收机制，属预期行为，不为此加断言**）

### U12：可选字段非 None 时正确透传

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 延伸 + Risk-7 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: 可选字段「非 None」路径（补 U5 的 None 路径，构成分支全覆盖） |
| Oracle | is 引用断言 |
| Mock | 否 — 哨兵 |

**等价类划分**：可选字段取值 → 代表值 = 「非 None 哨兵」（None 路径已由 U5 覆盖）

**Given**：
- `step_guard=S1`、`context_window_budget=B1`、`llm_settings=MS1`（三者非 None 哨兵）

**When**：
- `agent = blueprint.materialize()`

**Then**：
- `agent._loop._step_guard is S1`
- `agent._loop._context_window_budget is B1`
- `agent._loop._llm_settings is MS1`

### U13：clone 浅拷贝边界 — 列表独立、元素共享

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 边界 + Risk-8 [P2]（文档声明 YAGNI 的边界，测试固化边界防未来破坏） |
| 测试层级 | component(fake) |
| 覆盖准则 | N/A（浅拷贝语义验证） |
| Oracle | is 断言 + 副作用（列表长度变化） |
| Mock | 否 — 真实 CompositeAgentHooks |

**Given**：
- `hooks_template = CompositeAgentHooks()` 且 `add(hookA)`；`a1 = materialize()`、`a2 = materialize()`

**When**：
- 向 `a1._loop._hooks.add(hookB)`（公开注册方法 hooks.py:292）

**Then**：
- `a1._loop._hooks is not hooks_template`、`a1._loop._hooks is not a2._loop._hooks`（容器实例独立）
- `hooks_template._hooks` 长度不变（仍 1）、`a2._loop._hooks._hooks` 长度不变（仍 1）、`a1` 的为 2（**列表独立**）
- 边界声明断言：`a1._loop._hooks._hooks[0] is hooks_template._hooks[0]`（元素仍共享——浅拷贝边界，若某元素有跨会话 buffer 则污染，属文档已知 YAGNI）

### U14：aclose 正常关闭共享 client

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 + Risk-3 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: `close is None` 假分支 + try 正常路径 |
| Oracle | 副作用计数（FakeCloseLLMClient.aclose_calls） |
| Mock | 否 — 记录型 Fake |
| 确定性 | async 测试用 pytest-asyncio 事件循环，无真实 I/O |

**Given**：
- `llm_client = FakeCloseLLMClient()`（`aclose()` 计数、无异常）

**When**：
- `await blueprint.aclose()`

**Then**：
- 正常返回，不抛异常
- 副作用：`llm_client.aclose_calls == 1`

### U15：aclose — client 无 aclose 方法 → 静默返回

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 + Risk-3 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: `close is None` 真分支 |
| Oracle | golden（不抛异常即通过） |
| Mock | 否 — SentinelLLMClient（无任何方法） |

**Given**：
- `llm_client = SentinelLLMClient()`（无 aclose 属性）

**When**：
- `await blueprint.aclose()`

**Then**：
- 正常返回，不抛异常、无日志（静默）
- 副作用：无

### U16：aclose — client.aclose 抛异常 → best-effort warning，不阻断；重复调用幂等 [P1]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 + Risk-3 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: `except Exception` 真分支 |
| Oracle | caplog（日志级别 + 消息） + 副作用计数 |
| Mock | 否 — 故障注入 Fake + caplog |
| 故障注入 | 故障类型：`RuntimeError("close boom")`；注入点：`llm_client.aclose()`（每次调用都抛）；预期：blueprint 记 `logger.warning` 后正常返回，不 re-raise |
| 确定性 | caplog 按 logger name `"pandaren.agent.blueprint"` 过滤 |

**Given**：
- `llm_client = FakeCloseLLMClient(fail_on_close=RuntimeError("close boom"))`
- `caplog.set_level(logging.WARNING)`

**When**：
- `await blueprint.aclose()`；再次 `await blueprint.aclose()`（幂等验证）

**Then**：
- 两次均正常返回，不抛异常（关闭失败不阻断清理）
- caplog 中 `pandaren.agent.blueprint` 的 WARNING 记录共 2 条，均含 `"close boom"`
- 副作用：`llm_client.aclose_calls == 2`（**幂等 = blueprint 无内部关闭状态、每次都转发给底层；幂等性由底层 client 保证，见 §10 边界声明**）

### U17：repr 脱敏 — 格式正确、不含 api_key

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-9 + Risk-4 [P1]（凭证泄漏） |
| 测试层级 | unit |
| 覆盖准则 | N/A（纯输出格式化） |
| Oracle | golden value（repr 模板代码直读，可独立推导）+ property（不含 api_key） |
| Mock | 否 — FakeToolRegistry |

**等价类划分**：repr 输入组合 → 代表值 = (tools=2, stream=True) 与 (tools=0, stream=False) 两个变体（参数化）；agent_id 含特殊字符（`"agent-中文-01"`）验证 `!r` 引号处理

**Given**：
- `identity = FakeIdentity("agent-a")`
- `tool_registry = FakeToolRegistry([tool1, tool2])`（`list_tools()` 返回 2 个）
- `stream = True`

**When**：
- `repr(blueprint)`

**Then**：
- 返回值精确为 `"AgentBlueprint(identity='agent-a', tools=2, stream=True)"`（golden 全串）
- property：repr 字符串**不含** `"api_key"`、不含 llm_client 的 api_key 值（如 `"sk-1234"`）——即使把 api_key 属性放到 llm_client 哨兵上，repr 也不得出现
- 变体 (tools=0, stream=False)：repr 为 `"AgentBlueprint(identity='agent-a', tools=0, stream=False)"`

### U18：并发冒烟 — 多线程同时 materialize 保持共享/独立二分 [P3 可选]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 + inv-5 在并发下保持 + Risk-1 延伸 [P3] |
| 测试层级 | component(fake) |
| 覆盖准则 | N/A（并发冒烟，非完备并发测试） |
| Oracle | 集合级 is / is not 关系（不依赖线程调度顺序，确定性可控） |
| Mock | 否 — 哨兵 + 线程安全的 memory_factory（每次返回新 FakeMemory） |
| 确定性 | 断言只针对「最终产物集合关系」，不依赖执行顺序；blueprint frozen + 无内部可变状态，并发风险天然低，故定级 P3 |

**Given**：
- blueprint 全字段哨兵；`memory_factory` 每次返回新 `FakeMemory`

**When**：
- `ThreadPoolExecutor(8)` 并发执行 16 次 `materialize()`，收集 16 个 agent

**Then**：
- 任意两 agent 的 `_loop._memory` 两两 `is not`（互异）
- 任意两 agent 的 `_loop._hooks` 两两 `is not`
- 任意两 agent 的 `_loop._llm_client` 两两 `is`（同一共享引用）
- 副作用：memory_factory 共调用 16 次

---

## 9. 覆盖检查清单核对

| 类别 | 覆盖情况 |
|------|---------|
| Happy path | U6（materialize 成功装配）、U14（aclose 成功） |
| 边界值 | U5（可选字段 None 边界）、U12（非 None 边界）、U17（tools=0 边界） |
| 异常路径 | U1/U2/U3（构造校验）、U10/U11（工厂/clone 失败）、U16（关闭失败） |
| 副作用 | U9（顺序 log）、U14/U16（aclose 计数）、U13（列表长度变化） |
| 回滚/清理 | U10（clone 未执行=失败短路无残留）、U16（关闭失败不阻断后续清理）；memory 孤儿无回收机制，见 U11 备注 |
| 故障注入 | U10/U11/U16（注入点与预期行为已逐项声明） |
| 分支覆盖 | `__post_init__` 三异常分支 + happy 全覆盖；aclose 三分支全覆盖；materialize 两异常路径 + happy 全覆盖 |

豁免说明：本类无纯函数段（frozen 校验为构造期逻辑）；无真实外部 I/O，故 integration/e2e 不适用（理由见 §7 层级声明）。

---

## 10. known-gap 与边界声明

**known-gap：无。** 源码行为与需求文档契约一致（fail-safe 校验、5 步 materialize、aclose 契约、repr 脱敏均已按文档实现）。

**边界声明（非 gap，供评审确认）**：
1. `aclose()` 不维护自身幂等状态（无 `_closed` 标志），幂等完全委托底层 `llm_client.aclose()` 可重复调用（blueprint.py docstring 明示）。若未来某 client 的 aclose 非幂等，本契约即破坏——U16 已固化当前语义，改动需同步更新。
2. `materialize()` 中 memory_factory 成功后 clone 失败，已创建的 memory 成为无主对象（无回收机制）——属预期行为，U11 不为此加断言。
3. clone() 浅拷贝边界（hooks 元素共享）为本期文档声明的 YAGNI，U13 固化边界防无意破坏。

**推断项（请确认）**：pytest-asyncio 依赖（aclose 用例需要）；tests/ 目录落点（推断项目根，pytest 惯例）。

---

## 11. 给 test-coder 的衔接说明

- 本设计可直接落为 `tests/test_blueprint.py` + `tests/conftest.py`（Fake 对象）。
- U1 用 `@pytest.mark.parametrize("field_name", [...12 字段...])`，单函数 12 参数实例。
- U14-U16 用 `pytest.mark.asyncio`；U18 用 `concurrent.futures.ThreadPoolExecutor`（断言集合关系，不依赖调度）。
- 断言优先用 `is`/`is not`（引用）与 `==`（字符串），不引入 mock 库。
- 执行命令：`python -m pytest tests/test_blueprint.py -q`（P3 的 U18 可单独 `-m smoke` 标记按需执行）。
