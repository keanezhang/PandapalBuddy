# 测试设计：SubAgentRegistry / AgentLoader / AuditEventType 改动

- 目标源码：`pandaren/sub_agent/registry.py`、`pandaren/sub_agent/loader.py`、`pandaren/observability/types.py`
- 技术栈：pytest + pytest-asyncio（`pyproject.toml` 已配 `asyncio_mode = "auto"`，async 用例无需装饰器）
- 产出：本设计文档（不含可执行代码，落盘给下游 test-coder）

---

## 0. 改动点白盒定位（基于当前工作区源码，git 显示 3 文件均未提交）

| # | 改动 | 位置 | 行为 |
|---|------|------|------|
| C1 | register()：agent_name 唯一性（大小写不敏感） | registry.py:180-189 | 已存在 agent 的 `agent_name.strip().lower()` 与新 identity 相同 → 抛 `SubAgentRegistrationError` |
| C2 | register()：同 agent_id 二次注册按 source 优先级 | registry.py:163-178 | `identity.source <= existing_identity.source` → 抛错；高优先级 → pop 旧注册后覆盖 |
| C3 | `_execute_delegate()`：session_id 显式校验 | registry.py:501-511 | 在 Step 6 审计 `AGENT_DELEGATED` **之后**、push stack **之前**；`not session_id or not str(session_id).strip()` → 返回 `ToolResult(success=False)`，不再魔数兜底 |
| C4 | `_estimate_entry_tokens()`：优先注入 estimator | registry.py:704-729 | estimator 返回 `int > 0` → 采用；未注入/抛异常/非法返回值 → fallback `(len(id)+len(name)+len(desc)) // CHARS_PER_TOKEN + 5` |
| C5 | 审计映射：深度超限 | registry.py:757 | `"AGENT_DELEGATE_DEPTH_EXCEEDED" → AuditEventType.AGENT_DELEGATE_DEPTH_EXCEEDED`（此前误映射到 CYCLE） |
| C6 | `_parse_frontmatter()`：兼容 BOM + 前导空白 | loader.py:217-220 | 先剥 `\ufeff`，正则 `^[ \t\r\n]*---\s*\n...` 允许前导空行/空白 |
| C7 | `_parse_comma_list()`：非法类型告警 | loader.py:307-313 | 非 str/list/tuple（int/bool 等）→ 返回 `()` + `logger.warning`（此前静默） |
| C8 | `AuditEventType` 新增成员 | types.py:117 | `AGENT_DELEGATE_DEPTH_EXCEEDED = "agent_delegate_depth_exceeded"` |

**关键调用序（C3 依赖，白盒确认）**：`_execute_delegate` 顺序为
工厂查找 → 健康检查 → 信任校验 → 循环检测(Step4) → 深度检测(Step5) → 审计 AGENT_DELEGATED(Step6) → **session_id 校验(Step7)** → push stack → materialize/run → finally pop → 审计 AGENT_DELEGATE_COMPLETED(Step8)。
故 session_id 失败时：审计只有 DELEGATED、无 COMPLETED、无 materialize/run、stack 不被污染。

---

## 1. 不变式清单（Invariants）

| 编号 | 不变式 | 出处/依据 |
|------|--------|-----------|
| inv-1 | agent_id 是注册表唯一键 | registry.py:156-163 |
| inv-2 | agent_name 全局唯一（大小写不敏感、忽略首尾空白）——LLM 委派路由零歧义 | registry.py:180-189 |
| inv-3 | 同 agent_id 二次注册的最终态只能是「高 source 覆盖」或「拒绝」；低/同 source 绝不可能覆盖 | registry.py:165-178 |
| inv-4 | 注册表 version 守恒：register 成功 +1、失败不变 | registry.py:208 |
| inv-5 | 委派审计成对性：成功路径必含 AGENT_DELEGATED + AGENT_DELEGATE_COMPLETED；提前失败无 COMPLETED | registry.py:493-570 |
| inv-6 | SESSION_ID 契约 0 容忍：缺失/空白 → 显式失败，绝不魔数兜底；失败时无 materialize/run、无 stack 污染 | registry.py:501-511 |
| inv-7 | 深度超限审计事件类型恒为 AGENT_DELEGATE_DEPTH_EXCEEDED，不得落入 CYCLE | registry.py:757 |
| inv-8 | 摘要 token 估算恒为正（fallback 永不返回 0/负，阻止 1% 预算误判） | registry.py:720-729 |
| inv-9 | 解析器确定性：同一文本 → 同一 `(frontmatter, body)` | loader.py:205-244 |
| inv-10 | `_parse_comma_list` 非法类型 → `()` + warning（不静默） | loader.py:307-313 |
| inv-11 | AuditEventType 枚举成员取值唯一且稳定 | types.py:117 |

---

## 2. 风险清单与严重度（P0 优先测）

| 编号 | 风险 | 严重度×可能性 | 优先级 | 关联不变式 |
|------|------|--------------|--------|-----------|
| R1 | agent_name 重名（大小写差异被绕过）→ `_find_agent_id_by_name` 静默路由到第一个 → 委派到错误 Agent（逻辑错/资损级） | S高×L中 | **P0** | inv-2 |
| R2 | 同 agent_id 覆盖规则错误：低/同优先级静默覆盖高优先级 → 程序化蓝图被 SDK 内置覆盖，注册表静默不一致 | S高×L中 | **P0** | inv-3 |
| R3 | 覆盖路径状态残留：旧 status/factory/identity 未清理 → 幽灵状态（如 DRAINING 残留） | S中×L中 | P1 | inv-3 |
| R4 | `identity.source` 属性在生产路径缺失（见 Known-Gap KG-1）→ 覆盖分支真实路径 AttributeError，C2 功能失效 | S高×L中 | **P1** | inv-3 |
| R5 | session_id 缺失时魔数兜底 → 子 Agent 以假 session_id 记账，跨会话数据归属污染且静默 | S高×L中 | **P0** | inv-6 |
| R6 | session 校验的副作用失控：失败路径仍 materialize/run、污染 delegate stack、审计不完整 | S中×L中 | P1 | inv-5/inv-6 |
| R7 | 审计映射回归：深度超限事件又映射回 AGENT_DELEGATE_CYCLE → 安全告警/分析路由失真 | S中×L低 | P1 | inv-7 |
| R8 | 「0 容忍空值」误伤合法 falsy 值（如 session_id="0"）→ 正常委派被拒 | S中×L低 | P2 | inv-6 |
| R9 | estimator 估算失败未 fallback → 摘要构建整段阻断（1% 预算判定失效） | S中×L低 | P2 | inv-8 |
| R10 | estimator 返回非法值（0/负/非 int）被采用 → 预算量纲漂移 | S中×L低 | P2 | inv-8 |
| R11 | fallback 公式回归（chars/4+5）→ 与父级上下文预算不同尺 | S低×L低 | P3 | inv-8 |
| R12 | BOM/前导空行文件被误判「无 frontmatter」→ 蓝图加载抛 ValueError，Windows 记事本产物全挂 | S高×L中 | **P1** | inv-9 |
| R13 | `_parse_comma_list` 收到 int/bool 静默 → 配置错误被掩盖（如 `tools: 123` 静默变无工具，最小权限被意外放大/缩小） | S中×L低 | P2 | inv-10 |
| R14 | `AuditEventType` 缺 DEPTH 成员 → 映射 get 兜底到 AGENT_REGISTERED，深度超限以「注册」事件落库 | S中×L低 | P1 | inv-7/inv-11 |

非功能维度声明：并发/时序（ContextVar 隔离）非本次改动点，不做并发压测；性能/安全专项不展开。

---

## 3. Mock / Fake 策略

| 依赖 | 决策 | 理由 |
|------|------|------|
| AuditLog | **Fake**：`FakeAuditLog`（纯内存，`write_sync` 记录 `(event_type, agent_id, run_id, detail, step_n)` 到列表） | 审计断言只需事件序列，不需要真 backend 落盘 |
| blueprint.identity | **Fake**：`SimpleNamespace(agent_id, agent_name, source, trust_level)` 鸭子类型 | registry 只访问这 4 个属性；真实 `Identity` 无 `source`（KG-1），必须用 fake 才能测优先级语义 |
| blueprint.materialize / 目标 agent.run | **Fake**：计数 factory + 假 Agent（async run 返回固定结果） | 断言「materialize/run 未被调用」是 session 校验的核心副作用验证点 |
| TokenEstimator | **Fake**：可配置返回/抛异常、记录调用参数 | C4 的三条路径（正常/异常/非法返回）各需一个可控假件 |
| logger warning/debug | **caplog**（pytest 内建 fixture） | 断言 C7 告警留痕、C4 fallback 留痕 |
| SubAgentRegistry 自身方法 | **零 monkeypatch** | 只注入依赖，不 mock 被测方法，保证白盒语义被真实执行 |

---

## 4. 等价类划分汇总

### 4.1 register() 的 agent_id / agent_name / source 组合空间

```
agent_id 维度：  未注册 / 已注册(同 id)
source 维度：    新 source > 现有（覆盖合法）/ = 现有 / < 现有（均拒绝）
agent_name 维度：全同 / 大小写差异 / 首尾空白差异 / 完全不同
状态维度：       现有 agent 为 HEALTHY / DRAINING（仍占名）
```

### 4.2 session_id 等价类（C3 复合条件 `not session_id or not str(session_id).strip()`）

| 等价类 | 代表值 | 期望 |
|--------|--------|------|
| 属性缺失（getattr 默认 None） | 无 session_id 属性的 context | 失败 |
| None | `None` | 失败 |
| 空串 | `""` | 失败 |
| 纯空白 | `"   "`、`"\t\n"` | 失败 |
| 非空但 falsy 的字符串 | `"0"` | **通过**（0 容忍空值 ≠ 拒绝 falsy） |
| 非空 truthy | `"sess-1"`、`123`（int，str() 后非空） | 通过 |

MC-DC：子条件 A=`not session_id`、B=`not str(session_id).strip()`；用例覆盖 A=T、B=T / A=F,B=T / A=F,B=F 三组合（`"0"` 使 A=F,B=F）。

### 4.3 `_parse_comma_list` 输入空间

```
None / 空串 / 纯空白 → ()
str（逗号分隔，空项过滤 + strip）→ tuple[str]
list / tuple（逐项 str() + strip，空项过滤）→ tuple[str]
其他（int / bool / float / dict）→ () + warning   ← 本次改动新增分支
```
注意：`bool` 是 `int` 子类，必须单独覆盖（否则 `True` 会被误当 int 处理路径测不到）。

### 4.4 `_parse_frontmatter` 文本形态

```
严格首行 ---        （回归）
BOM(\ufeff) 前缀     （Windows 记事本）
BOM + 前导空行/空白
仅前导空行/空白（无 BOM）
无 frontmatter（含 BOM 与否）→ ({}, body)
YAML 损坏 → ({}, body) + warning
顶层非 dict（list 等）→ ({}, body) + warning
```

### 4.5 `_estimate_entry_tokens` estimator 状态空间

```
未注入(None) / 注入正常返回 int>0 / 返回 0 / 返回负 / 返回非 int(5.5) / 抛异常
```

---

## 5. 用例 × 风险覆盖矩阵

| 用例 | R1 | R2 | R3 | R4 | R5 | R6 | R7 | R8 | R9 | R10 | R11 | R12 | R13 | R14 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| REG-1 新注册成功 | | | | | | | | | | | | | | |
| REG-2 同 id，source≤ | | ✅ | ✅ | | | | | | | | | | | |
| REG-3 同 id，高 source 覆盖 | | ✅ | ✅ | | | | | | | | | | | |
| REG-4 不同 id 同名 | ✅ | | | | | | | | | | | | | |
| REG-5 覆盖 + 同名不自阻塞 | ✅ | ✅ | | | | | | | | | | | | |
| REG-6 DRAINING 仍占名 | ✅ | | | | | | | | | | | | | |
| DEL-1 session_id 空值族 | | | | | ✅ | ✅ | | | | | | | | |
| DEL-2 session_id="0"/123 | | | | | | | | ✅ | | | | | | |
| DEL-3 合法 session 全链路 | | | | | | ✅ | | | | | | | | |
| DEL-4 深度超限审计映射 | | | | | | | ✅ | | | | | | ✅ | |
| DEL-5 循环审计映射守卫 | | | | | | | ✅ | | | | | | | |
| TOK-1 estimator 正常 | | | | | | | | | | ✅ | | | | |
| TOK-2 estimator 抛异常 | | | | | | | | | ✅ | | | | | |
| TOK-3 estimator 非法返回值 | | | | | | | | | | ✅ | | | | |
| TOK-4 未注入 fallback | | | | | | | | | | | ✅ | | | |
| FM-1 严格首行回归 | | | | | | | | | | | | ✅ | | |
| FM-2 BOM/前导空白族 | | | | | | | | | | | | ✅ | | |
| FM-3 无 frontmatter | | | | | | | | | | | | ✅ | | |
| FM-4 YAML 损坏/非 dict | | | | | | | | | | | | ✅ | | |
| CL-1 None/空串/空白 | | | | | | | | | | | | | ✅ | |
| CL-2 str/list/tuple 合法 | | | | | | | | | | | | | ✅ | |
| CL-3 int/bool/float 告警 | | | | | | | | | | | | | ✅ | |
| CL-4 loader 组件级 tools:123 | | | | | | | | | | | | | ✅ | |
| TY-1 枚举成员存在 | | | | | | | | | | | | | | ✅ |

---

## 6. 用例详述（Given / When / Then）

> 通用测试夹具（写给 test-coder）：`make_blueprint(agent_id, agent_name, source, trust=TrustLevel.SUB_AGENT)` 返回 `SimpleNamespace(materialize=..., identity=SimpleNamespace(agent_id=..., agent_name=..., source=..., trust_level=...))`；`FakeAuditLog` 记录 `write_sync` 调用；`make_ctx(run_id, step_n, agent_id, session_id, trust_level=ORCHESTRATOR)` 返回真实 `ToolContext`（缺失 session_id 的变体用 `SimpleNamespace`）。

### 6.1 register()（C1/C2）—— unit

#### 用例 REG-1：新 agent_id + 唯一 agent_name 注册成功

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 / inv-2 / inv-4（成功路径基线）[P0 基础] |
| 测试层级 | unit |
| 覆盖准则 | branch: 无重复分支（dup-id=F, name-loop 未命中） |
| Oracle | golden value（状态值可手推） |
| Mock | 否 — FakeIdentity/FakeAuditLog 注入，不 mock 被测方法 |

**等价类划分**：agent_id 未注册 + agent_name 唯一 → 代表值 = `("coder", "代码审查", SubAgentSource.DIRECTORY)`

**Given**：
- `registry = SubAgentRegistry(audit_log=FakeAuditLog())`
- `bp = make_blueprint("coder", "代码审查", SubAgentSource.DIRECTORY)`

**When**：
- `registry.register(bp)`

**Then**：
- 返回值：None（无返回）
- 副作用：`registry.agent_count() == 1`；`registry.get_identity("coder").agent_name == "代码审查"`；`registry.get_status("coder") == AgentStatus.HEALTHY`；`registry.version == 1`
- 审计：`audit.events == [("AGENT_REGISTERED", "coder")]`（event_type 映射为 `AuditEventType.AGENT_REGISTERED`）

#### 用例 REG-2：同 agent_id 二次注册，source 不高于现有 → 拒绝且状态零变化

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R2 / R3 / inv-3 / inv-4 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: dup-id=T、`source <= existing` = T |
| Oracle | golden value |
| Mock | 否 — 同上 |

**等价类划分**：source 关系 ∈ {同优先级, 低优先级} → 代表值 = `(DIRECTORY→DIRECTORY, PROGRAMMATIC→DIRECTORY)`（参数化 2 组）

**Given**：
- `registry = SubAgentRegistry(audit_log=FakeAuditLog())`
- 先注册：`make_blueprint("coder", "代码审查", <existing_source>)`
- 记录 `version_before = registry.version`、`audit_len = len(audit.events)`

**When**：
- `registry.register(make_blueprint("coder", "新名称", <new_source>))`（同 id、source ≤ existing）

**Then**：
- 抛 `SubAgentRegistrationError`，异常消息含 `"已注册"` 与 `"coder"`
- 副作用（零变化）：`registry.agent_count() == 1`；`registry.get_identity("coder").agent_name == "代码审查"`（未被替换）；`registry.version == version_before`；`len(audit.events) == audit_len`（未写新 AGENT_REGISTERED）

#### 用例 REG-3：同 agent_id，高优先级（PROGRAMMATIC）覆盖低优先级（DIRECTORY）→ 替换且无残留

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R2 / R3 / inv-3 / inv-4 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: dup-id=T、`source <= existing` = F（覆盖分支） |
| Oracle | golden value |
| Mock | 否 — 同上 |

**等价类划分**：source 关系 = 高优先级唯一代表 = `DIRECTORY(1) → PROGRAMMATIC(2)`

**Given**：
- `registry = SubAgentRegistry(audit_log=FakeAuditLog())`
- 先注册 DIRECTORY 蓝图：`bp_old = make_blueprint("coder", "旧名称", DIRECTORY, marker="old")`；随后 `registry.set_status("coder", AgentStatus.DRAINING)`（制造残留，验证覆盖清场）
- 记录 `version_before`

**When**：
- `registry.register(make_blueprint("coder", "新名称", PROGRAMMATIC, marker="new"))`

**Then**：
- 无异常（覆盖成功）
- 副作用：
  - `registry.agent_count() == 1`（不重复计数）
  - `registry.get_identity("coder").agent_name == "新名称"`（identity 被替换）
  - `registry.get_status("coder") == AgentStatus.HEALTHY`（**旧 DRAINING 状态被清场重置**，验证 R3 无残留）
  - `registry._factories["coder"]().marker == "new"`（factory 被替换；白盒断言）
  - `registry.version == version_before + 1`
  - 审计：`audit.events` 含 2 条 `AGENT_REGISTERED`（首次 + 覆盖各一条）

#### 用例 REG-4：不同 agent_id、同名（exact / 大小写 / 首尾空白差异）→ 拒绝且状态零变化

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R1 / inv-2 / inv-4 [P0] |
| 测试层级 | unit |
| 覆盖准则 | branch: dup-id=F、name-loop 命中 |
| Oracle | golden value |
| Mock | 否 — 同上 |

**等价类划分**：name 差异维度 ∈ {全同, 仅大小写, 仅首尾空白} → 代表值 = `("代码审查"→"代码审查"), ("Code Review"→"code review"), ("  Code Review  "→"code review")`（参数化 3 组）

**Given**：
- 先注册：`registry.register(make_blueprint("a", <name_a>, DIRECTORY))`
- 记录 `version_before`、`audit_len`

**When**：
- `registry.register(make_blueprint("b", <name_b>, DIRECTORY))`（不同 agent_id、`name_b.strip().lower() == name_a.strip().lower()`）

**Then**：
- 抛 `SubAgentRegistrationError`，异常消息含新 agent_name 与被占用者 id `"a"`
- 副作用（零变化）：`agent_count() == 1`；`version == version_before`；`len(audit.events) == audit_len`；`get_identity("b") is None`

#### 用例 REG-5：同 agent_id 高 source 覆盖 + 同名 → 不自我阻塞（顺序回归守卫）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R1 / R3 / inv-2 / inv-3 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: 覆盖分支 + name-loop 不命中（旧 identity 先被 pop） |
| Oracle | golden value |
| Mock | 否 — 同上 |

**等价类划分**：覆盖场景中 name 相同 = 边界（若实现先查 name 再 pop，会误拒）→ 代表值 = `("coder", "代码审查", DIRECTORY→PROGRAMMATIC)`

**Given**：
- 先注册 DIRECTORY：`make_blueprint("coder", "代码审查", DIRECTORY)`

**When**：
- `registry.register(make_blueprint("coder", "代码审查", PROGRAMMATIC))`（同 id、同 name、高 source）

**Then**：
- 无异常（覆盖成功，不得因同名自我拒绝）
- 副作用：`agent_count() == 1`；`registry.version == 2`；`get_identity("coder").source == SubAgentSource.PROGRAMMATIC`

#### 用例 REG-6：现有 agent 为 DRAINING 仍占用 agent_name → 新 agent 同名被拒（全局唯一性）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R1 / inv-2 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: name-loop 对非 HEALTHY 身份同样命中 |
| Oracle | golden value |
| Mock | 否 — 同上 |

**等价类划分**：现有身份状态 ∈ {HEALTHY, DRAINING} 均占名 → 代表值 = DRAINING（边界）

**Given**：
- 注册 `("a", "审查专家")` 后 `registry.set_status("a", AgentStatus.DRAINING)`

**When**：
- `registry.register(make_blueprint("b", "审查专家", DIRECTORY))`

**Then**：
- 抛 `SubAgentRegistrationError`（DRAINING agent 恢复后仍具路由语义，名称不可被抢占）
- 副作用：`agent_count() == 1`；`version` 不变

### 6.2 `_execute_delegate()` session_id 校验（C3）—— component(fake)

> 公共 Given（除特别说明）：注册目标 `("coder", "代码审查", DIRECTORY)`；context 由 `make_ctx(run_id="r-caller", step_n=3, agent_id="caller", session_id=<x>, trust_level=ORCHESTRATOR)` 构造（ORCHESTRATOR 绕过信任校验干扰）；`FakeAuditLog` 注入。涉及 `_delegate_stack` seed 的用例用 `token = SubAgentRegistry._delegate_stack.set(...)` 并在 finally 中 `reset(token)`（确定性控制：同 task 内 seed/reset，防跨用例污染）。

#### 用例 DEL-1：session_id 缺失/None/空串/纯空白 → 显式失败，零副作用

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R5 / R6 / inv-5 / inv-6 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | MC-DC：复合条件 `A=not session_id`、`B=not str(session_id).strip()` 的 A=T,B=T / A=T,B=F / A=F,B=T 三组合全覆盖 |
| Oracle | golden value（ToolResult 字段 + 审计事件序列可手推） |
| Mock | 否 — FakeAuditLog + 计数 factory + 假 Agent |

**等价类划分**：session_id ∈ {属性缺失, `None`, `""`, `"   "`, `"\t\n"`}（参数化 5 组）

**Given**：
- 注册 `("coder", "代码审查", DIRECTORY)`；计数 factory（materialize 调用数 = 0）
- context：`make_ctx(...)` 的 session_id 取参数值；「属性缺失」组用 `SimpleNamespace(run_id="r-caller", step_n=3, agent_id="caller", trust_level=TrustLevel.ORCHESTRATOR)`（无 session_id 属性）
- 记录 `stack_before = SubAgentRegistry._delegate_stack.get(None)`、`version_before`

**When**：
- `result = await registry._execute_delegate("coder", "task", context)`

**Then**：
- 返回值：`result.success is False`；`"session_id" in result.error`；`result.tool_name == "call_agent"`；`result.data == ""`
- 副作用（零副作用证明，逐条断言）：
  - materialize 调用数 == 0（**未产出目标 Agent**）
  - 目标 run 调用数 == 0（未执行委派）
  - 审计 == `[AGENT_DELEGATED]` 一条（Step6 先于校验；**无** AGENT_DELEGATE_COMPLETED）
  - `SubAgentRegistry._delegate_stack.get(None)` 仍为 `stack_before`（stack 未被污染）
  - `registry.version == version_before`、`agent_count()` 不变

#### 用例 DEL-2：session_id 为 "0" / 123（非空 falsy 字符串 / 非 str）→ 不误伤，正常进入执行

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R8 / inv-6 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | MC-DC：A=F, B=F（两子条件均假 → 放行分支） |
| Oracle | golden value |
| Mock | 否 — 同上 |

**等价类划分**：非空但 falsy / 非 str 非空 → 代表值 = `"0"`、`123`（参数化 2 组；`str(123)="123"` 非空）

**Given**：
- 注册目标；计数 factory；假 Agent 的 `run()` 记录收到的 `session_id` 并返回 `SimpleNamespace(success=True, output="ok", error=None, run_id="run-1")`
- context：session_id = 参数值

**When**：
- `result = await registry._execute_delegate("coder", "task", context)`

**Then**：
- 返回值：`result.success is True`（"0" 是合法非空值，必须放行——0 容忍的是**空**不是 falsy）
- 副作用：materialize 调用数 == 1；假 Agent 收到的 `session_id == "0"`（int 组为 `"123"`，断言 str 化后非空并透传）；审计 == `[AGENT_DELEGATED, AGENT_DELEGATE_COMPLETED]`

#### 用例 DEL-3：session_id 合法 → 全链路成功，审计成对，stack 弹出

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R6 / inv-5 / inv-6（happy path 基线）[P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | statement/branch 全路径（含 finally pop） |
| Oracle | golden value |
| Mock | 否 — 同上 |

**等价类划分**：合法非空 session → 代表值 = `"sess-1"`

**Given**：
- 注册目标；假 Agent run 返回 `success=True, output="ok", error=None, run_id="run-1"` 并记录入参
- context：`make_ctx(..., session_id="sess-1")`

**When**：
- `result = await registry._execute_delegate("coder", "task", context)`

**Then**：
- 返回值：`result.success is True`；`"执行完成" in result.data`；`result.tool_name == "call_agent"`；`result.duration_ms >= 0`
- 副作用：
  - 假 Agent 收到的 `session_id == "sess-1"`（session_id 透传到目标 Agent 的 run——魔数兜底已根除的核心证明）
  - 审计 == `[AGENT_DELEGATED, AGENT_DELEGATE_COMPLETED]`（成对，inv-5）
  - `SubAgentRegistry._delegate_stack.get(None) is None`（finally pop，无栈残留）

#### 用例 DEL-4：委派深度超限 → ToolResult 失败 + 审计事件类型为 AGENT_DELEGATE_DEPTH_EXCEEDED（非 CYCLE）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R7 / inv-7 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: Step5 深度检测 T 分支 |
| Oracle | golden value（审计事件枚举可手推） |
| Mock | 否 — 同上（FakeAuditLog 捕获 event_type） |

**等价类划分**：stack 长度 ≥ max_delegate_depth（默认 1）→ 代表值 = seed stack `["reviewer"]`（**不能含目标 id**，否则先触发循环检测，测不到深度分支）

**Given**：
- 注册 `("coder", "代码审查", DIRECTORY)`（max_delegate_depth 默认 1）
- `token = SubAgentRegistry._delegate_stack.set(["reviewer"])`（seed 深度 1；finally 中 `reset(token)`）
- context：`make_ctx(..., session_id="sess-1")`（合法值，但深度检测在 session 校验之前）

**When**：
- `result = await registry._execute_delegate("coder", "task", context)`

**Then**：
- 返回值：`result.success is False`；`"深度超限" in result.error`
- 副作用（C5 核心断言）：
  - 审计事件序列 == `[AGENT_DELEGATE_DEPTH_EXCEEDED]`，其中 `event_type is AuditEventType.AGENT_DELEGATE_DEPTH_EXCEEDED`
  - `event_type is not AuditEventType.AGENT_DELEGATE_CYCLE`（防回归到旧映射）
  - materialize 调用数 == 0；无 COMPLETED 事件

#### 用例 DEL-5：循环委派 → 审计事件类型为 AGENT_DELEGATE_CYCLE（映射表回归守卫）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R7 / inv-7 [P2]（C5 改动的是同一张映射表，须证明 CYCLE 行未被误伤） |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: Step4 循环检测 T 分支 |
| Oracle | golden value |
| Mock | 否 — 同上 |

**等价类划分**：stack 含目标 id → 代表值 = seed stack `["coder"]`

**Given**：
- 注册 `("coder", ...)`；`token = SubAgentRegistry._delegate_stack.set(["coder"])`；finally reset

**When**：
- `result = await registry._execute_delegate("coder", "task", make_ctx(..., session_id="sess-1"))`

**Then**：
- 返回值：`result.success is False`；`"循环" in result.error`
- 副作用：审计 event_type `is AuditEventType.AGENT_DELEGATE_CYCLE`（且 `is not AGENT_DELEGATE_DEPTH_EXCEEDED`）

### 6.3 `_estimate_entry_tokens()`（C4）—— unit

> 直接调用私有方法（白盒 unit）：`registry = SubAgentRegistry()`；`fake_identity = SimpleNamespace(agent_id="a", agent_name="bb")`；`desc = "ccccccccc"`（9 字符）。fallback 手算：`(1+2+9) // 4.0 + 5 = 3.0 + 5 = 8.0`（`CHARS_PER_TOKEN=4.0` 为 float，见 KG-2）。

#### 用例 TOK-1：注入 estimator 正常返回 int>0 → 采用其值，调用参数精确

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R10 / inv-8 [P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: estimator 非 None 且返回合法 |
| Oracle | golden value |
| Mock | 否 — FakeTokenEstimator 注入 |

**等价类划分**：estimator ∈ {返回 int>0} → 代表值 = `123`

**Given**：
- `est = FakeTokenEstimator(return_value=123)`（记录 calls）；`registry = SubAgentRegistry(token_estimator=est)`；`desc = "cc"`

**When**：
- `tokens = registry._estimate_entry_tokens(fake_identity, desc)`

**Then**：
- 返回值：`tokens == 123`（采用 estimator，非 fallback 值 8）
- 副作用：`est.calls == [[{"role": "system", "content": "agent_name: bb\nwhen_to_use: cc"}]]`（恰 1 条 system message，内容含 agent_name 与 desc——与上下文预算同一把尺）

#### 用例 TOK-2：estimator 抛异常 → fallback + debug 留痕

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R9 / inv-8 [P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: except 分支 |
| Oracle | golden value |
| Mock | 否 — FakeTokenEstimator 注入（raise） |

**等价类划分**：estimator 行为 ∈ {抛异常} → 代表值 = `RuntimeError("boom")`

**Given**：
- `est = FakeTokenEstimator(raise_exc=RuntimeError("boom"))`；`registry = SubAgentRegistry(token_estimator=est)`；caplog 捕获

**When**：
- `tokens = registry._estimate_entry_tokens(fake_identity, "ccccccccc")`

**Then**：
- 返回值：`tokens == 8`（fallback，异常不阻断摘要构建）
- 副作用：caplog 有 debug 日志含 `"TokenEstimator 估算失败"`（留痕，inv-8 不静默）

#### 用例 TOK-3：estimator 返回 0 / 负 / 非 int → 非法值不采用，fallback

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R10 / inv-8 [P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: `isinstance(tokens,int) and tokens>0` = F（三个子分支） |
| Oracle | golden value |
| Mock | 否 — 同上 |

**等价类划分**：estimator 返回值 ∈ {0, -5, 5.5}（参数化 3 组：0 不满足 >0；-5 负；5.5 非 int）

**Given**：
- `est = FakeTokenEstimator(return_value=<param>)`；`registry = SubAgentRegistry(token_estimator=est)`

**When**：
- `tokens = registry._estimate_entry_tokens(fake_identity, "ccccccccc")`

**Then**：
- 返回值：`tokens == 8`（一律 fallback，inv-8：估算恒为正，杜绝 0/负污染 1% 预算判定）

#### 用例 TOK-4：未注入 estimator → fallback 字符粗估 golden 值

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R11 / inv-8 [P3] |
| 测试层级 | unit |
| 覆盖准则 | branch: estimator 为 None |
| Oracle | golden value（`(1+2+9)//4.0+5 = 8.0`，手算可推导，非跑实现抄来） |
| Mock | 否 — 零依赖 |

**等价类划分**：estimator ∈ {None} → 代表值 = 不传 `token_estimator`

**Given**：
- `registry = SubAgentRegistry()`（未注入 estimator）

**When**：
- `tokens = registry._estimate_entry_tokens(fake_identity, "ccccccccc")`

**Then**：
- 返回值：`tokens == 8`（8.0 == 8 成立；注：值为 float 8.0，KG-2）
- 副作用：无（纯计算）

### 6.4 `_parse_frontmatter()`（C6）—— unit

#### 用例 FM-1：严格 `---` 首行（回归基线）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R12 / inv-9 [P1]（对照，证明改动未破坏原行为） |
| 测试层级 | unit |
| 覆盖准则 | branch: 无 BOM、match 命中 |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：文本形态 = 严格首行 → 代表值 = `"---\nagent_id: reviewer\nagent_name: 审查专家\nwhen_to_use: 审查代码\n---\n你是一位审查专家"`

**Given**：无前置（纯函数直接调用）

**When**：
- `fm, body = _parse_frontmatter(<text>)`

**Then**：
- 返回值：`fm == {"agent_id": "reviewer", "agent_name": "审查专家", "when_to_use": "审查代码"}`；`body == "你是一位审查专家"`
- 副作用：无

#### 用例 FM-2：BOM / BOM+前导空行 / 仅前导空行 → 均正常解析（C6 核心）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R12 / inv-9 [P1] |
| 测试层级 | unit |
| 覆盖准则 | branch: BOM 剥离 T/F × 前导空白匹配（`[ \t\r\n]*`） |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：前缀形态 ∈ {`\ufeff`, `\ufeff\n\n  \n`, `\n\n  \n`}（参数化 3 组：仅 BOM / BOM+空行+空格 / 无 BOM 仅空行+空格）

**Given**：
- `text = prefix + "---\nagent_id: reviewer\nagent_name: 审查专家\nwhen_to_use: 审查代码\n---\n你是一位审查专家"`

**When**：
- `fm, body = _parse_frontmatter(text)`

**Then**：
- 返回值（3 组全同）：`fm == {"agent_id": "reviewer", "agent_name": "审查专家", "when_to_use": "审查代码"}`；`body == "你是一位审查专家"`
- 副作用：无

#### 用例 FM-3：无 frontmatter（含/不含 BOM）→ `({}, body)`，BOM 不泄漏进 body

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R12 / inv-9 [P2]（回归守卫） |
| 测试层级 | unit |
| 覆盖准则 | branch: match 未命中 |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：无 frontmatter ∈ {纯正文, BOM+纯正文} → 代表值 = `("纯正文文本", "\ufeff纯正文文本")`（参数化 2 组）

**Given**：无前置

**When**：
- `fm, body = _parse_frontmatter(text)`

**Then**：
- 返回值：`fm == {}`；`body == "纯正文文本"`（BOM 组：`body.startswith("\ufeff") is False`——BOM 已剥离且不残留）

#### 用例 FM-4：YAML 损坏 / 顶层非 dict → `({}, body)` + warning 留痕

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R12 / inv-9 [P2]（异常路径不崩溃、留痕） |
| 测试层级 | unit |
| 覆盖准则 | branch: yaml.YAMLError / 顶层非 dict |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock（caplog 捕获 warning） |

**等价类划分**：异常形态 ∈ {YAML 语法错, 顶层为 list} → 代表值 = `("---\nbad: [unclosed\n---\nbody", "---\n- a\n- b\n---\nbody")`（参数化 2 组）

**Given**：caplog 捕获

**When**：
- `fm, body = _parse_frontmatter(text)`

**Then**：
- 返回值：`fm == {}`；`body == "body"`
- 副作用：caplog warning——组1 含 `"YAML Frontmatter 解析失败"`；组2 含 `"顶层不是映射"`

### 6.5 `_parse_comma_list()`（C7）—— unit（CL-4 为 integration）

#### 用例 CL-1：None / 空串 / 纯空白 → `()` 且不告警

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-10（正常空态基线）[P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: None / str 空分支 |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：空值形态 ∈ {None, "", "   "} → 参数化 3 组

**When**：`_parse_comma_list(<param>, field_name="tools")`

**Then**：
- 返回值：`()`
- 副作用：caplog 无 warning 记录（合法空值不打扰）

#### 用例 CL-2：str / list / tuple 合法输入 → golden tuple

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-10（合法路径）[P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: str 切分 / list 逐项 / tuple 逐项 + 空项过滤 |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：合法形态 ∈ {str 逗号, 含空项 str, list 含空串, tuple 含非 str} → 代表值 = `("a, b ,c", "a,,b", ["a", " b ", ""], (1, "x"))`（参数化 4 组）

**When**：`_parse_comma_list(<param>, field_name="tools")`

**Then**：
- 返回值（逐组）：`("a","b","c")` / `("a","b")` / `("a","b")`（空串被过滤）/ `("1","x")`（非 str 项 `str()` 化）
- 副作用：无 warning

#### 用例 CL-3：int / float / bool（含 bool=int 子类边界）→ `()` + warning 含字段名与类型名（C7 核心）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R13 / inv-10 [P2] |
| 测试层级 | unit |
| 覆盖准则 | branch: 其他类型分支（**bool 单独覆盖**——它是 int 子类，若实现用 `isinstance(raw,int)` 会漏告警路径） |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock（caplog） |

**等价类划分**：非法类型 ∈ {int 42, float 3.14, bool True} → 参数化 3 组

**Given**：caplog 捕获

**When**：`_parse_comma_list(<param>, field_name="tools")`

**Then**：
- 返回值：`()`（3 组全同）
- 副作用：caplog warning 各 1 条，且同时含 `"tools"`（字段名）与类型名（组1 `"int"` / 组2 `"float"` / 组3 `"bool"`）——**不静默**（inv-10）

#### 用例 CL-4：loader 组件级——frontmatter `tools: 123`（YAML 还原为 int）→ 蓝图 tools 为空 + warning 传播

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R13 / inv-10 [P2]（C7 经真实 YAML 解析链路的端到端证明） |
| 测试层级 | integration（真实 tmp 文件 + 真实 PyYAML 解析） |
| 覆盖准则 | branch: loader 全链路过 C7 非法类型分支 |
| Oracle | golden value |
| Mock | 否 — 零 mock，真实文件 |

**等价类划分**：YAML 字段值 ∈ {int} → 代表值 = `tools: 123`

**Given**：
- `tmp_path / "agent.md"` 写入：
  ```yaml
  ---
  agent_id: t1
  agent_name: 测试
  when_to_use: 测试用
  trust_level: sub_agent
  tools: 123
  ---
  正文内容
  ```
- caplog 捕获

**When**：
- `bp = load_agent_from_file(str(tmp_path / "agent.md"))`

**Then**：
- 返回值：`bp.agent_id == "t1"`；`bp.tools == ()`（非法类型按空列表处理，最小权限 Fail-Safe 语义不放大）
- 副作用：caplog warning 含 `"tools"` 与 `"int"`（告警从解析层穿透到加载层，配置错误不再静默）

### 6.6 AuditEventType（C8）—— unit

#### 用例 TY-1：AGENT_DELEGATE_DEPTH_EXCEEDED 成员存在、值稳定、与 CYCLE 不同

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R14 / inv-11 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 枚举成员完整性（防成员缺失 → registry 映射兜底到 AGENT_REGISTERED） |
| Oracle | golden value |
| Mock | 否 — 零 mock |

**When**：
- `e = AuditEventType.AGENT_DELEGATE_DEPTH_EXCEEDED`

**Then**：
- 访问不抛 AttributeError（成员存在）
- `e.value == "agent_delegate_depth_exceeded"`
- `e is not AuditEventType.AGENT_DELEGATE_CYCLE`（取值唯一，inv-11）

---

## 7. 已知差距（Known-Gaps）

| 编号 | 相关用例 | 期望行为（设计立场） | 实现现状 | 差距原因 |
|------|----------|----------------------|----------|----------|
| KG-1 | REG-2/3/5 | 同 agent_id 二次注册按 source 优先级覆盖/拒绝 | **生产路径不可达**：`AgentBlueprint.identity` 是真实 `Identity`（identity/models.py:180-367，`__slots__` 5 字段），**无 `source` 属性**，且 `Identity.__setattr__` 抛 `PermissionError` 禁止事后注入 → registry.py:165 `identity.source` 抛 `AttributeError`，而非预期的 `SubAgentRegistrationError` 或覆盖 | C2 的实现假定 identity 携带 source，但改动未同步给 Identity；本设计用鸭子类型 FakeIdentity 锁定优先级语义，**真实路径需补丁**（给 Identity 加 `source` 字段，或 register() 改从 blueprint 取 source）。下游 test-coder 可对 REG-2/3 落 `pytest.xfail(..., strict=False)` 声明该差距，修复后「意外通过」即报警 |
| KG-2 | TOK-4 | fallback 返回 int | 实际返回 float（`// 4.0` → 3.0，+5 → 8.0）；docstring 声称 int | `CHARS_PER_TOKEN: float = 4.0`，整型整除遇 float 操作数返回 float；断言用 `== 8` 数值相等即可，若上层要求 `isinstance(int)` 需修源码。低风险，标注即可 |

---

## 8. 豁免与不在范围

| 维度 | 处置 |
|------|------|
| 故障注入 | 本改动全部为纯逻辑/编排层：REG/FM/CL/TOK 无外部 I/O 豁免；DEL 的外部依赖（factory 产出真实 Agent、AuditLog 落盘）均被 Fake 替代，故障注入属于真实 Agent 层与 backend 层测试，不在本改动范围 |
| 副作用验证 | REG/DEL/TOK 全部必填（状态/审计/调用计数）；FM/CL 为纯函数，写明「无副作用」豁免 |
| 并发/时序 | `_delegate_stack` 为 ContextVar，隔离性非本次改动点；用例仅需保证同 task 内 seed/reset（已写入 Given 确定性控制），不做并发压测 |
| 非功能（性能/安全） | 按角色边界仅识别不展开：session_id 契约属数据完整性（P0 已覆盖）；无性能敏感点 |
| 状态机转换表 | 本改动不构成状态机（register 是原子校验+写入，delegate 是顺序 guard 链），用分支覆盖即可；非法的「低优先级覆盖」已作为 REG-2 分支覆盖 |
| Pairwise | register 的 3 个维度（id/source/name）是顺序 guard 而非并行参数组合，无相互作用缺陷风险，不适用 pairwise，按等价类直列 |

---

## 9. 汇总

- 用例数：24（REG-1~6 / DEL-1~5 / TOK-1~4 / FM-1~4 / CL-1~4 / TY-1）
- 层级分布：unit ×19、component(fake) ×4、integration ×1
- 覆盖准则：register 分支全覆盖；session 复合条件达 MC-DC；loader 分支全覆盖
- 关键决策：零 mock（全 Fake 注入）；source 优先级语义用鸭子类型 FakeIdentity 锁定并标 KG-1；golden value 全部可手推（无自指 oracle）；fallback 估算值经手算推导（12//4+5=8）
