# 测试设计：`Agent`（pandaren/agent/agent.py）

被测对象：`pandaren.agent.agent.Agent` —— SDK 顶层 Agent 运行时类（转发层）。
`Agent` 是**编排/转发层**：除 `provider`/`model_name` 的属性推导与 `aclose` 的幂等标志外，
几乎全部行为 = 把调用原样转发给 `_loop`（AgentLoop）。因此本设计围绕
「转发是否原样、不遗漏、不错位」「Fail-Safe 分支是否真的不阻断」「生命周期是否幂等」展开。

---

## 0. 信息收集与前置声明

| 项 | 来源 | 状态 |
|----|------|------|
| 函数签名 | agent.py 白盒（已读全文 212 行） | ✅ |
| 依赖签名 | run_core.py `run`/`run_stream`、memory.py `set_system_prompt`、identity/models.py（已核对） | ✅ |
| 测试框架 | 未指定 → 推断 pytest（Python 项目） | ⚠️ 推断，请确认 |
| Mock 政策 | 编排层 → 零 mock 框架，全部 Fake（见 §3） | 推断，符合方法论 |

**推断确认点**：项目无既有 `__tests__` 目录，本文档按约定输出到 `pandaren/agent/__tests__/design/`；
若团队有统一测试目录约定，请主 Agent 迁移。

---

## 1. 白盒分析摘要（关键发现）

### 1.1 `provider` / `model_name` 表达式解析（agent.py:77-78, 89-90）

```python
client = getattr(self._loop, "_llm_client", None)
return getattr(client, "provider", "") or "" if client is not None else ""
```

Python 优先级：条件表达式低于 `or`，故等价于 `(getattr(client, "provider", "") or "") if client is not None else ""`。
分支矩阵（`provider` 与 `model_name` 结构完全相同，仅属性名不同）：

| 分支 | 触发条件 | 结果 |
|------|---------|------|
| **A** | `client is None`（`_loop` 无 `_llm_client` 属性 **或** 属性值为 None） | `""`（Fail-Safe） |
| **B** | `client` 存在且 `client.provider` 为 truthy 非空串 | `client.provider` 原值 |
| **C** | `client` 存在且 `client.provider` 为 falsy（`""` 或 `None`） | `""` |
| **D** | `client` 存在但无 `provider` 属性 | `getattr` 默认 `""` → `""` |

> C/D 殊途同归（都返回 `""`），但**触发路径不同**（C 走属性值、D 走 getattr 缺省），须分别覆盖——
> D 分支若实现者把 `getattr` 默认值误改成抛异常，只有 D 用例能抓住。

### 1.2 转发签名比对（防遗漏/错位的关键依据）

| 方法 | Agent 转发参数（全部 keyword） | 底层签名参数 | 差异 |
|------|------------------------------|-------------|------|
| `run()` | task, session_id, resume_state, hitl_decision, interaction_response, metadata, skill_name | 同左 + **settings** | ⚠️ Agent.run 不暴露 `settings`（见 §9 观察点 O-1） |
| `run_stream()` | task, session_id, resume_state, hitl_decision, interaction_response, metadata, skill_name, **plan_action, edited_plan_content, settings** | 完全一致（10 参数） | 无 |
| `cancel()` | 无参 → `_loop.cancel()` | 同步方法 | 无 |
| `rebind_system_prompt()` | prompt → `_loop._memory.set_system_prompt(prompt)` | 同步方法 | 访问 `_loop._memory` 私有属性 |
| `aclose()` | 仅置 `_closed` 标志，**不触碰 `_loop`/`_llm_client`** | — | 幂等 |
| `__aexit__` | → `await self.aclose()` | — | 无 |

全部转发为 **keyword-only 传参** → 「错位」风险天然被语法消除，真正风险是**参数遗漏**（少传一个）与**篡改**（传了别的值）→ 用例用「全量 kwargs 快照比对」证明。

### 1.3 确定性控制

本类无时间/随机数/时区/迭代顺序依赖（`__repr__` 只读 identity 字符串、转发无排序）。
Fake 协作者全确定性 → **无 flaky 来源**，无需钉时钟。Fake 记录用列表追加，断言前不排序（追加顺序即调用顺序，本身就是要验证的副作用）。

---

## 2. 不变式与风险清单

### 不变式（Invariants）

- **inv-1 转发不变式**：`Agent.run` / `Agent.run_stream` 收到的每个参数（含默认值 None）必须**原样**到达 `_loop` 同名参数，不多、不少、不改。
- **inv-2 引用透传**：`run()` 返回值、`run_stream()` 每个 yield 事件必须是 `_loop` 产出的**同一对象**（Agent 层零包装、零拷贝）。
- **inv-3 Fail-Safe 不变式**：`provider` / `model_name` 在 `_llm_client` 不可达（None / 无属性 / 空值）时恒返回 `""`，**永不抛异常**（不阻断 run 启动前的预算检查）。
- **inv-4 aclose 幂等**：`aclose` 多次调用无副作用（第二次起直接 return）；且 **不关闭** `_loop` 及共享 `_llm_client`。
- **inv-5 生命周期**：`__aenter__` 返回自身；`__aexit__`（无论正常/异常退出）必定调用一次 `aclose`。
- **inv-6 只读身份**：`identity` / `agent_id` / `agent_name` 与构造注入值恒等（identity 不可变，HC1）。

### 风险清单（按优先级排序）

| # | 风险 | 严重度 | 可能性 | 优先级 |
|---|------|:--:|:--:|:--:|
| Risk-1 | Fail-Safe 分支被破坏：client 不可达时抛 AttributeError / 返回 None，导致预算拦截逻辑崩溃或类型错 | 高（阻断 run 或类型破坏） | 中（纯透传模式为常规路径） | **P0** |
| Risk-2 | `run`/`run_stream` 参数遗漏或篡改（少传 skill_name、传错 metadata 等），下游 HITL/恢复/审计行为错 | 中高（行为错、恢复数据错位） | 中 | **P1** |
| Risk-3 | `aclose` 幂等被破坏：二次调用重复执行清理 / 误关共享 `_llm_client`，驱逐单个实例连带杀死全进程 HTTP 连接池 | 高（共享资源被误伤，其余 session「client has been closed」） | 低 | **P1** |
| Risk-4 | `run_stream` 事件被包装/拷贝，调用方按 `is` 或身份匹配事件时失效 | 中 | 低 | **P2** |
| Risk-5 | `__aexit__` 漏调 `aclose`（异常路径未走 finally 风格清理），资源泄漏 | 中 | 低 | **P2** |
| Risk-6 | `cancel` / `rebind_system_prompt` 转发目标错（如访问了错误的 loop 属性），取消/人格切换失效 | 中 | 低 | **P2** |
| Risk-7 | `__repr__` 格式偏离（字段顺序、引号），日志/调试解析依赖此格式 | 低 | 低 | **P3** |
| Risk-8 | `AgentStatus` 枚举值被改（"healthy"→其他），下游序列化/状态机比对错 | 低 | 低（静态定义） | **P3** |
| Risk-9 | identity 直通属性绕开注入值（改了内部引用） | 低 | 低（纯 property） | **P3** |

**非功能范围声明**：性能（转发延迟、stream 吞吐）、安全（prompt 注入）非本设计范围——
Agent 为薄转发层，无自身算法；此类风险归属 loop 层专项，不在此假装覆盖。

---

## 3. Mock / Fake 策略

**零 mock 框架，全部用内存 Fake**。理由：Agent 是编排层，其可测行为 = 与协作者的交互序列（调用次数、参数快照、产出对象）；
Fake 内存实现既能验证全部副作用，又不引入 mock 库的耦合。`Agent.__init__` 只要求 `loop` 鸭子类型（运行时不检查 `AgentLoop` 类型），Fake 可无缝注入。

| 依赖 | 决策 | 理由 |
|------|------|------|
| `Identity` | 真实现 | 轻量纯值对象，构造 5 参数即可，无 I/O |
| `_loop`（FakeLoop） | **Fake** | 鸭子类型 stub，仅实现 Agent 访问的成员：`_llm_client`、`run`、`run_stream`、`cancel`、`_memory`；记录 `run_calls`（(task, kwargs) 列表）、`run_stream_calls`、`cancel_calls`；`run` 返回预置 `AgentResult`，`run_stream` 依次 yield 预置 `StreamEvent` 列表 |
| `_loop._llm_client`（FakeClient） | **Fake** | 可配置 `provider` / `model_name` 属性；`close()` 记录 `close_calls`（验证 aclose 不触碰它） |
| `_loop._memory`（FakeMemory） | **Fake** | `set_system_prompt(prompt)` 记录调用参数与次数 |
| `RunState` / `ModelSettings` | 真类型或简单实例 | 纯数据对象，仅作透传载荷（用最小合法实例即可，内容无关） |

**测试层级判据**：无真实进程边界 I/O（Fake 替代一切协作者）→ 除标注 unit 的用例（仅依赖注入的纯值对象、无协作）外，一律 **component(fake)**。

---

## 4. 等价类划分总表

| 维度 | 等价类 | 代表值 |
|------|--------|--------|
| `_llm_client` 状态 | 存在且有属性值 / 存在但属性为空串 / 存在但属性为 None / 存在但无该属性 / 不存在（无 `_llm_client` 属性或 None） | 见各用例 |
| `task` | 普通字符串 / 空串 | `"帮我写首诗"` / `""` |
| `session_id` | 非空字符串（必传） | `"sess-001"` |
| `resume_state` | None（新对话）/ RunState 实例（恢复） | `None` / 最小 `RunState` 实例 |
| `hitl_decision` | None / "approved" / "rejected" | 透传用例取 `"approved"` |
| `interaction_response` | None / 非空 str | `"是的，继续"` |
| `metadata` | None / dict | `{"tenant": "acme"}` |
| `skill_name` | None / 非空 str | `"code_review"` |
| `plan_action` | None / "approve" | 透传用例取 `"approve"` |
| `edited_plan_content` | None / 非空 str | `"修订后的计划正文"` |
| `settings` | None / ModelSettings 实例 | 最小 `ModelSettings()` 实例 |
| `prompt`（rebind） | 非空 str（内容不处理，纯透传） | `"你是代码评审专家"` |
| aclose 调用次数 | 1 次 / 多次（2~3 次） | 见用例 |

> 透传参数**内容本身不被 Agent 处理**（只透传），故每个参数只需 1 个非默认代表值 + 全默认组，
> 不按内容细分——内容语义属于 loop 层测试范围。核心风险是「**全部同时传非默认值时是否每个都到达**」。

---

## 5. 用例 × 风险覆盖矩阵

| 用例 | inv-1 转发 | inv-2 引用 | inv-3 Fail-Safe | inv-4 幂等 | inv-5 生命周期 | inv-6 只读 | Risk-1 | Risk-2 | Risk-3 | Risk-4 | Risk-5 | Risk-6 | Risk-7 | Risk-8 | Risk-9 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| 1. AgentStatus 枚举 | | | | | | | | | | | | | | ✅ | |
| 2. identity 直通 | | | | | | ✅ | | | | | | | | | ✅ |
| 3. provider 有值 | | | ✅ | | | | ✅ | | | | | | | | |
| 4. provider client=None | | | ✅ | | | | ✅ | | | | | | | | |
| 5. provider 空串 | | | ✅ | | | | ✅ | | | | | | | | |
| 6. provider 无属性 | | | ✅ | | | | ✅ | | | | | | | | |
| 7. model_name 有值 | | | ✅ | | | | ✅ | | | | | | | | |
| 8. model_name client=None | | | ✅ | | | | ✅ | | | | | | | | |
| 9. model_name 空串 | | | ✅ | | | | ✅ | | | | | | | | |
| 10. run 全参数透传 | ✅ | ✅ | | | | | | ✅ | | | | | | | |
| 11. run 全默认透传 | ✅ | | | | | | | ✅ | | | | | | | |
| 12. run 异常冒泡 | | | | | | | | | | | | | | | |
| 13. run_stream 全参数透传 | ✅ | ✅ | | | | | | ✅ | | ✅ | | | | | |
| 14. run_stream 全默认透传 | ✅ | | | | | | | ✅ | | | | | | | |
| 15. run_stream 惰性 | ✅ | | | | | | | ✅ | | | | | | | |
| 16. aclose 幂等 | | | | ✅ | | | | | ✅ | | | | | | |
| 17. aclose 不关共享 client | | | | ✅ | | | | | ✅ | | | | | | |
| 18. aclose 后功能不受影响 | | | | ✅ | | | | | ✅ | | | | | | |
| 19. cancel 转发 | ✅ | | | | | | | | | | | ✅ | | | |
| 20. rebind_system_prompt 转发 | ✅ | | | | | | | | | | | ✅ | | | |
| 21. __aenter__/__aexit__ 正常退出 | | | | ✅ | ✅ | | | | | | ✅ | | | | |
| 22. __aexit__ 异常退出仍 aclose | | | | ✅ | ✅ | | | | | | ✅ | | | | |
| 23. __repr__ 格式 | | | | | | | | | | | | | ✅ | | |

豁免声明：本类无外部 I/O 依赖（网络/磁盘/DB/MQ）→ **故障注入类不适用**（理由：故障注入的注入点都在 `_loop` 内部，属 loop 层测试范围；Agent 层唯一可注入的是「FakeLoop 抛异常」，已由用例 12 覆盖）。副作用验证不豁免——所有转发用例均验证 Fake 记录（调用次数/参数快照）。

---

## 6. 用例详设

### 公共 Given 说明（Fake 协作者契约，所有用例复用）

- **FakeClient**：可配置 `provider`/`model_name` 属性（含删除属性的能力，用于 D 分支）；`close()` 自增 `close_calls`。
- **FakeLoop**：持有 `_llm_client`、`_memory`；`run(task, **kwargs)` 把 `(task, kwargs)` 追加进 `run_calls` 并返回预置 `result`；`run_stream(task, **kwargs)` 追加进 `run_stream_calls` 并逐个 yield 预置 `events`；`cancel()` 自增 `cancel_calls`。**不定义 close 方法**（若 Agent 误调会 AttributeError，本身即失败信号）。
- **FakeMemory**：`set_system_prompt(prompt)` 记录 `(prompt, call_count)`。
- **Identity**（真实）：`Identity(agent_id="agent-001", agent_name="主助手", when_to_use="执行主任务", sensitive_permissions=frozenset(), trust_level=TrustLevel.ORCHESTRATOR)`。
- **Agent 构造**：`Agent(identity=identity, loop=fake_loop)`。

---

#### 用例1：AgentStatus 枚举成员与值

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-8 枚举值漂移 [P3] |
| 测试层级 | unit |
| 覆盖准则 | N/A（静态枚举定义） |
| Oracle | golden value（规格白纸黑字，人可推导） |
| Mock | 否 — 纯定义，零 mock |

**等价类划分**：三个成员各自独立断言 → 代表值 = 枚举名与 `.value` 一一对应

**Given**：无前置（直接引用 `AgentStatus`）

**When**：读取三个枚举成员

**Then**：
- `AgentStatus.HEALTHY` 是 `AgentStatus` 成员，`.value == "healthy"`
- `AgentStatus.UNHEALTHY` 是成员，`.value == "unhealthy"`
- `AgentStatus.DRAINING` 是成员，`.value == "draining"`
- 副作用：无（静态定义）

---

#### 用例2：identity / agent_id / agent_name 只读直通

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 只读身份 + Risk-9 直通绕行 [P3] |
| 测试层级 | unit |
| 覆盖准则 | N/A（纯 property 直通） |
| Oracle | golden value（注入值即期望值） |
| Mock | 否 — identity 为真实纯值对象 |

**等价类划分**：三个属性各自代表注入值 → 代表值 = `agent-001` / `主助手`

**Given**：
- 注入 `identity = Identity(agent_id="agent-001", agent_name="主助手", ...)`（见公共 Given）
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- 依次读取 `agent.identity`、`agent.agent_id`、`agent.agent_name`

**Then**：
- `agent.identity is identity`（引用相等，非拷贝）
- `agent.agent_id == "agent-001"`
- `agent.agent_name == "主助手"`
- 副作用：无（只读访问，Fake 无调用）

---

#### 用例3：provider — client 存在且有值 → 原值返回

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 Fail-Safe + Risk-1 [P0]（B 分支：正常透传） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: B（client 非 None 且 provider truthy） |
| Oracle | golden value（注入值即期望） |
| Mock | 否 — FakeClient 为内存对象 |

**等价类划分**：`_llm_client` 存在 + `provider` 非空 → 代表值 = `"dashscope"`

**Given**：
- `fake_client.provider = "dashscope"`
- `fake_loop._llm_client = fake_client`
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `agent.provider`

**Then**：
- 返回值 = `"dashscope"`
- 副作用：无（纯属性推导）

---

#### 用例4：provider — client 为 None（Fail-Safe 分支）→ 返回 "" 不抛异常

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 Fail-Safe + Risk-1 [P0]（A 分支，最关键的 Fail-Safe） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: A（`client is None`） |
| Oracle | golden value（规格明确：底层不可达 → `""`） |
| Mock | 否 — Fake 零 mock |

**等价类划分**：`_llm_client` 不可达，含两个触发子类：① `fake_loop` 无 `_llm_client` 属性；② `fake_loop._llm_client = None` → 取 ② 为代表

**Given**：
- `fake_loop` 不带 `_llm_client` 属性（或设为 None）
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `agent.provider`

**Then**：
- 返回值 = `""`（Fail-Safe：不阻断 run 前预算检查）
- 不抛 `AttributeError` / `TypeError`
- 副作用：无

---

#### 用例5：provider — client 存在但 provider 为空串 → ""

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 Fail-Safe + Risk-1 [P0]（C 分支） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: C（属性存在但 falsy，`"" or "" → ""`） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：属性存在且为 falsy：`""`（空串）与 `None` 同归此分支 → 取 `""` 为代表

**Given**：
- `fake_client.provider = ""`
- `fake_loop._llm_client = fake_client`
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `agent.provider`

**Then**：
- 返回值 = `""`
- 副作用：无

---

#### 用例6：provider — client 存在但无 provider 属性 → ""（getattr 缺省兜底）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 Fail-Safe + Risk-1 [P0]（D 分支，防 getattr 缺省被改坏） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: D（getattr 走默认值路径） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：属性不存在（`del fake_client.provider`）→ 代表值 = 无该属性

**Given**：
- `fake_client` 构造时不定义 `provider` 属性（或 `del fake_client.provider`）
- `fake_loop._llm_client = fake_client`
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `agent.provider`

**Then**：
- 返回值 = `""`（getattr 默认值生效，未抛 AttributeError）
- 副作用：无

---

#### 用例7：model_name — client 存在且有值 → 原值返回

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 Fail-Safe + Risk-1 [P0]（B 分支） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: B |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：`model_name` 非空 → 代表值 = `"qwen-plus"`

**Given**：
- `fake_client.model_name = "qwen-plus"`；`fake_loop._llm_client = fake_client`
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `agent.model_name`

**Then**：
- 返回值 = `"qwen-plus"`
- 副作用：无

---

#### 用例8：model_name — client 为 None → "" 不抛异常

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 Fail-Safe + Risk-1 [P0]（A 分支） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: A |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：`_llm_client` 不可达（无属性或 None）→ 代表值 = None

**Given**：
- `fake_loop._llm_client = None`；`agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `agent.model_name`

**Then**：
- 返回值 = `""`；不抛异常
- 副作用：无

---

#### 用例9：model_name — client 存在但 model_name 为空串 → ""

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 Fail-Safe + Risk-1 [P0]（C 分支；与 provider 同表达式，独立验证防属性名笔误） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: C |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：`model_name` 为 falsy（`""`/None）→ 代表值 = `""`

**Given**：
- `fake_client.model_name = ""`；`fake_loop._llm_client = fake_client`
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `agent.model_name`

**Then**：
- 返回值 = `""`
- 副作用：无

---

#### 用例10：run() — 全部参数同时传非默认值，逐一原样到达 _loop

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 转发 + inv-2 引用 + Risk-2 参数遗漏/篡改 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 语句覆盖：run() 全转发行（N/A 无分支；核心是「全量快照比对」而非逐参数抽查） |
| Oracle | golden value（期望 kwargs 快照由人工从调用现场推导：入参必须原样出现在 Fake 记录中） |
| Mock | 否 — FakeLoop 捕获调用 |

**等价类划分**：全部 7 参取非默认代表值（见 §4）→ 一次调用覆盖「每个参数都到达」的交互缺陷；全默认场景由用例 11 覆盖

**Given**：
- `fake_loop.run` 预置返回 `AgentResult(success=True, run_id="r-1", output="ok", ...)`（最小合法实例）
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `result = await agent.run("帮我写首诗", session_id="sess-001", resume_state=<RunState 实例>, hitl_decision="approved", interaction_response="是的，继续", metadata={"tenant": "acme"}, skill_name="code_review")`

**Then**：
- 返回值：`result is fake_loop 预置的同一 AgentResult 对象`（inv-2：零包装）
- 副作用：`fake_loop.run_calls` 长度为 1，其 `(task, kwargs)` 为：
  - `task == "帮我写首诗"`
  - `kwargs == {resume_state: <同一 RunState 实例>, session_id: "sess-001", hitl_decision: "approved", interaction_response: "是的，继续", metadata: {"tenant": "acme"}, skill_name: "code_review"}`（全量 dict 相等，缺一不可）

---

#### 用例11：run() — 全部参数走默认值，None 也如实透传

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 转发 + Risk-2 [P1]（防「默认值被实现者吞掉/改写」） |
| 测试层级 | component(fake) |
| 覆盖准则 | 语句覆盖（默认值路径） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：`resume_state`/`hitl_decision`/`interaction_response`/`metadata`/`skill_name` 全部 None → 代表值 = 缺省调用

**Given**：
- `fake_loop.run` 预置返回最小 `AgentResult`
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `await agent.run("hello", session_id="sess-002")`（其余参数不传）

**Then**：
- 返回值：`result is` Fake 预置对象
- 副作用：`fake_loop.run_calls[0]` 的 kwargs == `{resume_state: None, session_id: "sess-002", hitl_decision: None, interaction_response: None, metadata: None, skill_name: None}`

---

#### 用例12：run() — 底层异常原样冒泡（Agent 不吞、不包装）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | 异常路径（Agent 层职责边界：O3「永远返回 AgentResult」是 loop 层的保证，Agent 不重复实现也不吞）[P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: 异常传播路径 |
| Oracle | golden value（异常类型与消息原样） |
| Mock | 否 |

**Given**：
- `fake_loop.run` 抛 `RuntimeError("boom")`（模拟 loop 内部故障）
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `await agent.run("t", session_id="s-1")`

**Then**：
- 抛出 `RuntimeError`，消息为 `"boom"`（类型与消息均未被包装/改写）
- 副作用：`fake_loop.run_calls` 长度 1（异常发生在 loop 内，转发本身已发生）

---

#### 用例13：run_stream() — 全部 10 参数透传 + 事件原样逐个 yield

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 转发 + inv-2 引用 + Risk-2 + Risk-4 事件被包装 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 语句覆盖（全转发行，含 plan_action/edited_plan_content/settings 三个独有参数） |
| Oracle | golden value（kwargs 快照）+ 引用相等（事件身份） |
| Mock | 否 |

**等价类划分**：10 参全取非默认代表值（task/session_id/resume_state/hitl_decision/interaction_response/metadata/skill_name + plan_action="approve"/edited_plan_content="修订后的计划正文"/settings=<ModelSettings 实例>）

**Given**：
- `fake_loop.run_stream` 预置 yield 两个事件：`[StreamEvent(type=LLM_TOKEN, ...), StreamEvent(type=RUN_END, ...)]`（最小合法实例）
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `events = [e async for e in agent.run_stream("帮我写首诗", session_id="sess-001", resume_state=<RunState 实例>, hitl_decision="approved", interaction_response="是的，继续", metadata={"tenant": "acme"}, skill_name="code_review", plan_action="approve", edited_plan_content="修订后的计划正文", settings=<ModelSettings 实例>)]`

**Then**：
- 事件列表：`events == [预置事件1, 预置事件2]`，且逐个 `is` 同一对象（顺序一致、零包装）
- 副作用：`fake_loop.run_stream_calls` 长度 1，`kwargs` 含全部 10 个参数且与入参逐字段相等（含 `plan_action="approve"`、`edited_plan_content="修订后的计划正文"`、`settings is` 同一 ModelSettings 实例）

---

#### 用例14：run_stream() — 全部默认值如实透传

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 转发 + Risk-2 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 语句覆盖（默认值路径） |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：7 个可选参数全部 None → 代表值 = 缺省调用

**Given**：
- `fake_loop.run_stream` 预置 yield 空列表
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `async for _ in agent.run_stream("hello", session_id="sess-002"): pass`

**Then**：
- 副作用：`fake_loop.run_stream_calls[0]` 的 kwargs == `{resume_state: None, session_id: "sess-002", hitl_decision: None, interaction_response: None, metadata: None, skill_name: None, plan_action: None, edited_plan_content: None, settings: None}`

---

#### 用例15：run_stream() — 惰性：不消费 generator 不触发底层调用

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 转发 + Risk-2 [P2]（async generator 语义保持） |
| 测试层级 | component(fake) |
| 覆盖准则 | 语句覆盖（生成器惰性语义） |
| Oracle | 蜕变关系（调用→不执行；消费→执行） |
| Mock | 否 |

**Given**：
- `agent = Agent(identity=identity, loop=fake_loop)`；`fake_loop.run_stream_calls` 初始为空

**When**：
- `gen = agent.run_stream("t", session_id="s-1")`（只创建 generator，不迭代）

**Then**：
- `fake_loop.run_stream_calls == []`（函数体未执行）
- `gen` 是 async generator 对象（有 `__anext__`）
- 继续 `await gen.__anext__()`（或 async for）后，`run_stream_calls` 长度变为 1

---

#### 用例16：aclose() — 幂等：多次调用无副作用 [property]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 幂等 + Risk-3 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: `if self._closed: return` 的真假两侧各走到 |
| Oracle | property（幂等性：`aclose∘aclose == aclose`，状态不变量）——建议下游用 fast-check 对「随机次数(1..5) 连续调用」断言无异常且 `_closed` 恒 True |
| Mock | 否 |

**Given**：
- `agent = Agent(identity=identity, loop=fake_loop)`；`agent._closed` 初始为 False

**When**：
- `await agent.aclose()` 连续调用 3 次

**Then**：
- 三次均正常返回（不抛异常）
- `agent._closed == True` 且始终为 True（首次置位后不再变化）
- 副作用：`fake_loop` 无任何新调用记录（无 close 相关调用——FakeLoop 未定义 close，若被误调会 AttributeError）

---

#### 用例17：aclose() — 绝不关闭共享 llm_client（所有权边界）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 幂等 + Risk-3 误关共享 client [P1]（资源所有权：client 由调用方注入并共享，谁创建谁关闭） |
| 测试层级 | component(fake) |
| 覆盖准则 | 副作用验证（关键） |
| Oracle | golden value（副作用计数为 0） |
| Mock | 否 — FakeClient.close 计数 |

**Given**：
- `fake_client` 带 `close()`（`close_calls` 自增）；`fake_loop._llm_client = fake_client`
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `await agent.aclose()`（一次）

**Then**：
- `fake_client.close_calls == 0`（共享 client 未被触碰）
- `fake_loop` 无 close 相关调用（FakeLoop 未定义 close，未抛 AttributeError 即证明未被调用）

---

#### 用例18：aclose() — 关闭后其余转发职能不受影响

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 幂等 + Risk-3 [P2]（aclose 只清自身状态，不阉割 Agent 职能） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: `_closed=True` 状态下其他方法路径 |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `agent = Agent(identity=identity, loop=fake_loop)`；`await agent.aclose()`（先关闭）
- `fake_loop.run` 预置返回最小 `AgentResult`

**When**：
- `await agent.run("t", session_id="s-1")`

**Then**：
- 正常返回 Fake 预置的 `AgentResult`（`_closed` 不阻塞 run 转发）
- 副作用：`fake_loop.run_calls` 长度 1（aclose 未破坏 _loop 引用）

---

#### 用例19：cancel() — 转发到 _loop.cancel()

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 转发 + Risk-6 转发目标错 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 语句覆盖（单行转发） |
| Oracle | golden value（调用计数） |
| Mock | 否 |

**Given**：
- `agent = Agent(identity=identity, loop=fake_loop)`；`fake_loop.cancel_calls == 0`

**When**：
- `agent.cancel()`

**Then**：
- `fake_loop.cancel_calls == 1`
- 无返回值要求（方法返回 None，可顺带断言）

---

#### 用例20：rebind_system_prompt() — 转发到 _loop._memory.set_system_prompt()

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 转发 + Risk-6 [P2]（访问 `_loop._memory` 私有属性的路径） |
| 测试层级 | component(fake) |
| 覆盖准则 | 语句覆盖（单行转发） |
| Oracle | golden value（prompt 原值 + 调用计数） |
| Mock | 否 — FakeMemory 记录 |

**等价类划分**：prompt 内容不处理（纯透传）→ 代表值 = `"你是代码评审专家"`

**Given**：
- `fake_loop._memory = fake_memory`（FakeMemory，`set_system_prompt_calls` 初始为 0）
- `agent = Agent(identity=identity, loop=fake_loop)`

**When**：
- `agent.rebind_system_prompt("你是代码评审专家")`

**Then**：
- 副作用：`fake_memory` 收到 `("你是代码评审专家", 1)`（prompt 原值 + 恰好调用 1 次）

---

#### 用例21：__aenter__ 返回自身；__aexit__ 正常退出时调用 aclose

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 生命周期 + Risk-5 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: 正常退出路径（exc_type=None） |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `agent = Agent(identity=identity, loop=fake_loop)`；`agent._closed` 初始 False

**When**：
- `async with agent as ctx:`（块内不做任何事，正常退出）

**Then**：
- `ctx is agent`（`__aenter__` 返回同一实例）
- 块退出后 `agent._closed == True`（`__aexit__` 调用了 `aclose`）
- 副作用：无异常抛出

---

#### 用例22：__aexit__ — 块内抛异常时仍调用 aclose（清理不缺席）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 生命周期 + Risk-5 异常路径漏清理 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: 异常退出路径（exc_type 非 None） |
| Oracle | golden value |
| Mock | 否 |

**Given**：
- `agent = Agent(identity=identity, loop=fake_loop)`；`agent._closed` 初始 False

**When**：
- `async with agent: raise ValueError("inner")`（块内抛异常）

**Then**：
- 异常原样向外传播（`ValueError("inner")`，`__aexit__` 不吞异常）
- 块退出后 `agent._closed == True`（异常路径同样执行了 `aclose`）

---

#### 用例23：__repr__ 输出格式

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-7 格式漂移 [P3] |
| 测试层级 | unit |
| 覆盖准则 | N/A（纯格式化） |
| Oracle | golden value（格式 `Agent(id='{agent_id}', name='{agent_name}')` 人可推导） |
| Mock | 否 |

**等价类划分**：agent_id/agent_name 普通值（无引号）→ 代表值 = `agent-001` / `主助手`

**Given**：
- `agent = Agent(identity=identity, loop=fake_loop)`（identity 见公共 Given）

**When**：
- `repr(agent)`

**Then**：
- 返回值 = `"Agent(id='agent-001', name='主助手')"`
- 副作用：无

---

## 7. 覆盖准则汇总

| 准则 | 落点 |
|------|------|
| 分支覆盖（provider/model_name 全 4 分支） | 用例 3-9 |
| 分支覆盖（aclose 幂等 if） | 用例 16、18 |
| 分支覆盖（__aexit__ 正常/异常） | 用例 21、22 |
| 语句覆盖（转发行全量快照） | 用例 10、11、13、14、15、19、20 |
| property（幂等、惰性） | 用例 15、16 |

> 本类无复合条件判定（无 `a && b || c`），MC/DC 不适用。

---

## 8. 豁免声明（显式）

| 覆盖类别 | 结论 | 理由 |
|---------|------|------|
| 故障注入（网络/DB/MQ/磁盘） | **不适用** | Agent 层无外部 I/O；所有外部依赖在 `_loop` 内部。Agent 层唯一「故障」注入点是 FakeLoop 抛异常 → 已由用例 12 覆盖 |
| 回滚/清理 | 部分适用 | Agent 无事务；唯一清理语义是 `aclose`（幂等 + 不关共享资源）→ 用例 16-18 |
| 并发/时序 | 不适用 | Agent 无共享可变状态（`_closed` 为单实例标志）；loop 的并发隔离（session_id）属 loop 层范围 |

---

## 9. Known-Gap 与观察点

| # | 类型 | 用例/位置 | 期望行为 | 实际现状 | 说明 |
|---|------|----------|---------|---------|------|
| O-1 | 接口差异（非缺陷） | 用例 10/11 | — | `Agent.run()` 签名**不含** `settings`，而 `AgentLoop.run()`（run_core.py:256）含 | 任务范围明确 run() 透传仅 5 参，属既定接口。若下游期望非流式也支持 settings，需扩展 `Agent.run` 签名——**不迁就实现、也不擅自改设计**，仅记录 |
| O-2 | 观察点 | 用例 23 | — | `__repr__` 未转义 agent_id/agent_name 中的单引号（如 name 含 `'` 会破坏格式） | 低风险（P3），日志调试用；若需严格可后续加转义。未设专项用例以免充数 |
| O-3 | 观察点 | 用例 4 | — | `_llm_client` 不可达时 `provider`/`model_name` 返回 `""`，调用方若把 `""` 当有效 provider 落库会产生空值 | 注释声明「缺失即 bug、无默认兜底」的语义由应用层承担，Agent 层行为符合规格 |

---

## 10. 修订记录

- v1（本次）：基于 agent.py 全文白盒 + run_core.py/memory.py/identity/models.py 签名核对产出 23 用例。
