# 设计文档：pandaren/hook/hooks.py（AgentHooks / DefaultAgentHooks / CompositeAgentHooks）

- 被测文件：`pandaren/hook/hooks.py`（543 行，21 个 hook 扩展点）
- 被测类：`AgentHooks`（Protocol）、`DefaultAgentHooks`、`CompositeAgentHooks`
- 测试框架：**推断 pytest**（项目现有 `pandaren/observability/tests/*` 均为 pytest 风格）——此处为推断，请确认
- Mock 政策：算法/编排层零 mock；仅 inv-3 用 monkeypatch 包装 `inspect.signature` 做**探测计数**（非行为替身）
- 版本：v1（初始设计）

---

## 1. 白盒分析摘要

### 1.1 21 个 hook 的三类转发模式（决定用例怎么分组，而非 21 个各写一遍）

| 模式 | 方法 | Composite 转发行为 |
|------|------|--------------------|
| 模式 A：run 级固定转发（15 个） | on_run_start / on_run_end / on_step_start / on_step_end / on_before_tool_call / on_after_tool_call / on_tool_discover / on_tool_disabled / on_concurrent_execution_failure / on_hitl_requested / on_hitl_resolved / on_error / on_halt / on_skill_activated / on_skill_cleared | 全部固定参数 + `session_id=session_id` |
| 模式 B：provider 条件转发（2 个） | on_before_llm_call / on_after_llm_call | kwargs 恒含 model/tools/call_type/session_id（after 为 model/duration_ms/call_type/session_id）；`provider` 仅当 `_accepts(h, method, "provider")` 为 True 时追加 |
| 模式 C：非 run 级固定转发（4 个） | on_tool_register / on_tool_circuit_open / on_tool_circuit_close / on_tool_output_truncated | 无 session_id、无 provider，仅转发声明参数 |

### 1.2 关键实现事实（白盒，供断言依据）

- **异常隔离**：21 个方法均为 `for h in self._hooks: try: ... except Exception: _logger.debug(...)`。`except Exception` **不捕获 BaseException**（KeyboardInterrupt/SystemExit 会传播）。
- **_accepts**：key=`(id(hook), method_name, param)`；命中判定 `if cached is not None`（**False 也命中缓存**）；`ok = param in sig.parameters or any(VAR_KEYWORD)`；`except (TypeError, ValueError, AttributeError): ok = True`（inspect 失败假定接受）。
- **clone()**：`new_composite._hooks = list(self._hooks)`；`_sig_cache` 为新实例 `__init__` 的空 dict（天然独立）。
- **add()**：`if hooks is not None: self._hooks.append(hooks)` —— None 被静默忽略。
- **run 级 17 个**带 `session_id`（A/B/C/D/E 部分/F 并发失败/G/H），**非 run 级 4 个**不带（on_tool_register + F 三个 harness 事件）。
- `ObservabilityHooksAdapter` 实现 **19/21** 个 hook（缺 on_skill_activated/cleared）——见 Known-Gap KG-1。

### 1.3 21 方法统一参数表（供 U1/U7/U8 参数化复用）

| # | 方法 | 调用参数（位置 + 关键字，测试统一值） | run 级 |
|---|------|--------------------------------------|:--:|
| 1 | on_run_start | task=`"task-1"`, run_id=`"run-1"`, session_id=`"sess-42"` | ✅ |
| 2 | on_run_end | run_id=`"run-1"`, success=`True`, terminal_reason=`"done"`, session_id=`"sess-42"` | ✅ |
| 3 | on_step_start | step_n=`1`, run_id=`"run-1"`, session_id=`"sess-42"` | ✅ |
| 4 | on_step_end | step_n=`1`, run_id=`"run-1"`, session_id=`"sess-42"` | ✅ |
| 5 | on_before_llm_call | messages=`[{"role":"user","content":"hi"}]`, run_id=`"run-1"`, model=`"gpt-4o"`, tools=`None`, call_type=`"main"`, session_id=`"sess-42"`, provider=`"openai"` | ✅ |
| 6 | on_after_llm_call | response=`{"text":"hi"}`, run_id=`"run-1"`, model=`"gpt-4o"`, duration_ms=`12.3`, call_type=`"main"`, session_id=`"sess-42"`, provider=`"openai"` | ✅ |
| 7 | on_before_tool_call | tool_name=`"web_search"`, args=`{"q":"x"}`, run_id=`"run-1"`, step_n=`1`, session_id=`"sess-42"` | ✅ |
| 8 | on_after_tool_call | tool_name=`"web_search"`, result=`{"ok":True}`, run_id=`"run-1"`, step_n=`1`, duration_ms=`5.0`, session_id=`"sess-42"` | ✅ |
| 9 | on_tool_register | tool_name=`"web_search"`, tier=`"standard"`, sensitivity=`"high"`, namespace=`None` | ❌ |
| 10 | on_tool_discover | tool_name=`"web_search"`, query=`"x"`, run_id=`"run-1"`, session_id=`"sess-42"` | ✅ |
| 11 | on_tool_disabled | tool_name=`"web_search"`, reason=`"denied"`, run_id=`"run-1"`, session_id=`"sess-42"` | ✅ |
| 12 | on_tool_circuit_open | tool_name=`"web_search"`, failure_count=`3`, recovery_timeout=`60.0` | ❌ |
| 13 | on_tool_circuit_close | tool_name=`"web_search"` | ❌ |
| 14 | on_tool_output_truncated | tool_name=`"web_search"`, original_size=`1000`, max_size=`500` | ❌ |
| 15 | on_concurrent_execution_failure | tool_names=`["a","b"]`, run_id=`"run-1"`, step_n=`1`, session_id=`"sess-42"` | ✅ |
| 16 | on_hitl_requested | tool_name=`"approve_loan"`, run_id=`"run-1"`, session_id=`"sess-42"` | ✅ |
| 17 | on_hitl_resolved | tool_name=`"approve_loan"`, decision=`"approved"`, run_id=`"run-1"`, session_id=`"sess-42"` | ✅ |
| 18 | on_error | error=`RuntimeError("boom")`, run_id=`"run-1"`, session_id=`"sess-42"` | ✅ |
| 19 | on_halt | reason=`"user_stop"`, run_id=`"run-1"`, session_id=`"sess-42"` | ✅ |
| 20 | on_skill_activated | skill_name=`"math"`, skill_type=`"ACTION"`, tools=`["calc"]`, run_id=`"run-1"`, step_n=`1`, session_id=`"sess-42"` | ✅ |
| 21 | on_skill_cleared | skill_name=`"math"`, run_id=`"run-1"`, session_id=`"sess-42"` | ✅ |

---

## 2. 不变式清单

| 编号 | 不变式 | 优先级 | 性质 |
|------|--------|:--:|------|
| inv-1 | Composite 单 hook 抛异常 → 后续 hook 仍被调用、异常不外抛 | P0 | 容错 |
| inv-2 | `_accepts` 对声明 provider 的 hook 传 provider；未声明（旧签名）不传且调用成功；`**kwargs` 兜底视为接受 | P0 | 向后兼容 |
| inv-3 | `_sig_cache` 命中后不再重复 inspect（False 结果同样命中缓存） | P0 | 缓存正确性/性能 |
| inv-4 | `clone()` 后两实例 `_hooks` 列表相互独立（add 不影响对方）、元素引用共享、`_sig_cache` 独立 | P0 | 隔离性 |
| inv-5 | run 级 hook 的 session_id 原样透传给每个子 hook | P0 | 数据隔离 |
| inv-6 | 21 个 hook 方法齐全：AgentHooks / DefaultAgentHooks / CompositeAgentHooks 方法名集合一致且均为 21；run 级 17 个带 session_id、非 run 级 4 个不带 | P0 | 契约完整性 |
| inv-7 | DefaultAgentHooks 实例满足 AgentHooks Protocol（runtime_checkable isinstance） | P0 | 协议一致性 |
| inv-8 | hook 按 add 顺序执行 | P0 | 顺序 |
| inv-9 | 非 run 级 hook（on_tool_register / on_tool_circuit_* / on_tool_output_truncated）不接收 session_id（签名无此参数，转发不传） | P1 | 设计约束 |
| inv-10 | AgentHooks 是 Protocol（不可直接实例化） | P1 | 类型约束 |
| inv-11 | Composite 空列表（未 add）调用任意 hook 不抛异常、无副作用 | P1 | 空态健壮性 |

---

## 3. 风险清单（按优先级排序）

| 编号 | 风险 | S×L | 优先级 | 关联不变式 |
|------|------|-----|:--:|------|
| R1 | 单 hook 抛异常中断链 → 观测数据残缺（logs/traces 缺段） | 高×中 | P0 | inv-1 |
| R2 | on_before/after_llm_call 新增 provider 后，旧签名 hook 被强制传 provider → TypeError → 被 Composite 静默吞掉 → 用户 hook 回调**失效且无感知** | 高×高 | P0 | inv-2 |
| R3 | `_accepts` 每次调用重复 inspect（性能退化）或缓存误命中（key 碰撞） | 中×中 | P0 | inv-3 |
| R4 | clone 后共享 `_hooks` 列表 → 一个 session 增删 hook 影响其他 session（跨会话污染） | 高×中 | P0 | inv-4 |
| R5 | session_id 漏传/错传 → 观测数据跨会话混片（数据隔离破坏） | 高×中 | P0 | inv-5 |
| R6 | 三实现方法集合漂移（增删 hook 时某处漏改）→ 运行时 AttributeError / 扩展点缺失 | 高×中 | P0 | inv-6 |
| R7 | DefaultAgentHooks 不满足 Protocol → `isinstance` 运行时检查失败 | 中×低 | P0 | inv-7 |
| R8 | 调用顺序 ≠ add 顺序 → 观测前置/后置逻辑错乱 | 中×中 | P0 | inv-8 |
| R9 | 调用方误向非 run 级 hook 传 session_id → TypeError 被吞 → 回调失效 | 中×中 | P1 | inv-9 |
| R10 | 误把 AgentHooks 当普通类实例化（`AgentHooks()`） | 低×低 | P1 | inv-10 |
| R11 | 无 hooks 场景（builder 早期/测试桩）调 Composite 崩溃 | 中×低 | P1 | inv-11 |
| R12 | `add(None)` 注入 None 元素 → 后续转发 AttributeError | 中×低 | P2 | — |
| R13 | 参数透传不完整（漏传 model/tools/call_type 等）→ 观测缺字段 | 中×低 | P2 | — |
| R14 | BaseException（KeyboardInterrupt/SystemExit）被吞 → 中断无法传播 | 低×低 | P3 | —（预期：传播，设计确认项） |

> 不适用维度：并发/时序、状态机、故障注入（本模块无外部 I/O、无状态机、无并发状态）——Composite 为同步纯转发，唯一"故障"是子 hook 抛异常（R1 已覆盖）。确定性：无时间/随机/浮点参与断言（集成用例不断言 duration 值），无需冻结时钟。

---

## 4. 测试层级与 Mock/Fake 策略

| 被测对象 | 层级 | 依赖处理 |
|---------|------|---------|
| AgentHooks（Protocol 约束） | unit | 无依赖 |
| DefaultAgentHooks | unit | 无依赖 |
| CompositeAgentHooks 全部行为 | component(fake) | Fake：`RecordingHook` / `ThrowingHook`（内存记录调用，零 mock） |
| Composite + ObservabilityHooksAdapter 真实链式 | integration | 真实 adapter + **内存 backend**（InMemoryLogger/Tracer/MetricsBackend）注入，避免磁盘 I/O 与 flaky |

**Fake 设计（component 用例统一使用）**：
- `RecordingHook(name)`：每个方法记录 `(method_name, kwargs)` 到 `self.calls` 列表；不抛异常。
- `ThrowingHook(name)`：指定某方法抛 `RuntimeError("boom")`，其余方法记录。

---

## 5. Oracle 策略

| 用例类别 | Oracle | 说明 |
|---------|--------|------|
| Fake 调用序列/参数断言 | 副作用 oracle | 直接断言 RecordingHook.calls 的具体条目与字段值 |
| `_accepts` 返回值 | golden value | True/False 可由签名直接推导（旧签名无 provider → False；新签名 → True） |
| 方法集合（inv-6） | golden value | 21 个方法名 + run/非 run 归属可由规格文本（hooks.py 注释分区）独立推导 |
| 参数透传（U15） | 参考行为 | 调用值即期望值（identity 透传），非"跑实现抄值" |

无需蜕变关系（所有输出可预知/可直断）。

---

## 6. 覆盖矩阵（用例 × 不变式）

| 用例 | inv-1 | inv-2 | inv-3 | inv-4 | inv-5 | inv-6 | inv-7 | inv-8 | inv-9 | inv-10 | inv-11 | R12 | R13 | R14 | 层级 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|------|
| U1 异常隔离（参数化 21 方法） | ✅ | | | | | | | | | | | | | | component(fake) |
| U2 provider 条件转发 | | ✅ | | | | | | | | | | | | | component(fake) |
| U3 kwargs 兜底 + inspect 失败 | | ✅ | | | | | | | | | | | | | component(fake) |
| U4 _sig_cache 命中不重复 inspect | | | ✅ | | | | | | | | | | | | component(fake) |
| U5 clone _hooks 列表独立 | | | | ✅ | | | | | | | | | | | component(fake) |
| U6 clone 元素共享 + 缓存独立 | | | | ✅ | | | | | | | | | | | component(fake) |
| U7 session_id 透传（参数化 17 run 级） | | | | | ✅ | | | | | | | | | | component(fake) |
| U8 21 方法集合 + 签名约束 | | | | | | ✅ | | | | | | | | | unit |
| U9 Default 满足 Protocol | | | | | | | ✅ | | | | | | | | unit |
| U10 add 顺序执行 | | | | | | | | ✅ | | | | | | | component(fake) |
| U11 非 run 级不接收 session_id | | | | | | | | | ✅ | | | | | | unit + component(fake) |
| U12 AgentHooks 不可实例化 | | | | | | | | | | ✅ | | | | | unit |
| U13 空列表调用不抛异常 | | | | | | | | | | | ✅ | | | | component(fake) |
| U14 add(None) 忽略 | | | | | | | | | | | | ✅ | | | component(fake) |
| U15 参数透传完整性 | | | | | | | | | | | | | ✅ | | component(fake) |
| U16 BaseException 传播边界 | | | | | | | | | | | | | | ✅ | component(fake) |
| U17 真实 adapter 链式（integration） | ✅ | ✅ | | | ✅ | | | | | | | | ✅ | | integration |

---

## 7. 用例详设

---

#### 用例 U1：单 hook 抛异常 → 后续 hook 仍被调用、异常不外抛（参数化全部 21 个方法）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 [P0] + R1 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：21 个方法各自的 `except Exception` 分支（每方法至少走到一次 catch） |
| Oracle | 副作用 oracle（Fake 记录） |
| Mock | 否 — Fake 零 mock |

**等价类划分**：异常位置 ∈ {首元素}（首元素抛 = 最坏情况：若实现无保护，后续全部中断，最能暴露 R1）→ 代表值 = 首元素 `ThrowingHook`、次元素 `RecordingHook`

**Given**（前置条件）：
- 构造 `composite = CompositeAgentHooks()`
- `composite.add(ThrowingHook("h1"))`（h1 的所有方法抛 `RuntimeError("boom")`）
- `composite.add(RecordingHook("h2"))`
- 参数按 §1.3 参数表，对第 i 个方法取第 i 行参数

**When**（操作/动作）：
- 对 21 个方法逐一调用：`getattr(composite, method)(**params)`（参数化，一方法一子用例）

**Then**（预期结果）：
- 调用不抛任何异常（外抛被抑制）
- 副作用：`h2.calls` 恰含 1 条 `(method, params)`，且 `params` 中无额外/缺失字段（与 §1.3 表逐键一致）
- 例外预期：非 run 级 4 个方法（#9/#12/#13/#14）的 `params` 中**不含** session_id 键（inv-9，防断言写错）

---

#### 用例 U2：provider 条件转发 —— 声明 provider 的 hook 收到 provider；旧签名 hook 不传且调用成功

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 [P0] + R2 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：on_before_llm_call / on_after_llm_call 的 `if self._accepts(...)` 真/假两路（本用例覆盖假路 = 旧签名） |
| Oracle | 副作用 oracle + golden value（_accepts=False 可推导） |
| Mock | 否 |

**等价类划分**：
- hook 签名维度：{声明 provider} / {未声明 provider（旧签名）} / {**kwargs 兜底（U3 覆盖）}
- 本用例：前两类，各代表值 = `NewSigHook`（on_before/after 签名含 `provider: str = ""`）、`OldSigHook`（签名**不含** provider）

**Given**（前置条件）：
- `composite.add(NewSigHook("new"))`；`composite.add(OldSigHook("old"))`

**When**（操作/动作）：
- `composite.on_before_llm_call(messages=[{"role":"user","content":"hi"}], run_id="run-1", model="gpt-4o", tools=None, call_type="main", session_id="sess-42", provider="openai")`
- 再以同样策略调用 `on_after_llm_call(response={"text":"hi"}, run_id="run-1", model="gpt-4o", duration_ms=12.3, call_type="main", session_id="sess-42", provider="openai")`

**Then**（预期结果）：
- `new.calls[-1]["kwargs"]["provider"] == "openai"`（新签名收到 provider）
- `old.calls[-1]["kwargs"]` 中**无** `provider` 键，且其余键与调用值一致（旧签名未收到 provider，**调用成功未抛 TypeError**）
- `_accepts(old, "on_before_llm_call", "provider") is False`；`_accepts(new, "on_before_llm_call", "provider") is True`（golden value 直断）

---

#### 用例 U3：`**kwargs` 兜底视为接受；inspect 失败假定接受

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 [P0] + R2 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`_accepts` 的 VAR_KEYWORD 分支；`except (TypeError, ValueError, AttributeError)` 分支 |
| Oracle | golden value（True）+ 副作用 oracle |
| Mock | 否（inspect 失败场景用不可内省的 callable 触发真实异常，非 mock） |

**等价类划分**：
- `**kwargs` 兜底：代表值 = `KwargsHook`（on_before/after 签名 `def on_before_llm_call(self, messages, run_id, **kwargs)`）
- inspect 失败：代表值 = `UninspectableHook`（其方法为 C 扩展式 callable——测试可用 `object()` 冒充方法：`getattr` 返回非 callable 时 `inspect.signature` 抛 TypeError；或构造 `__signature__` 缺失的 callable 实例）

**Given**（前置条件）：
- `composite.add(KwargsHook("kw"))`；`composite.add(UninspectableHook("un"))`

**When**（操作/动作）：
- `composite.on_before_llm_call(messages=[{"role":"user","content":"hi"}], run_id="run-1", model="gpt-4o", tools=None, call_type="main", session_id="sess-42", provider="openai")`

**Then**（预期结果）：
- `_accepts(kwargs_hook, "on_before_llm_call", "provider") is True`（VAR_KEYWORD 兜底）
- `kw.calls[-1]["kwargs"]["provider"] == "openai"`（兜底 hook 收到 provider，走正常路径）
- `un.calls[-1]["kwargs"]["provider"] == "openai"`（inspect 失败 → 假定接受 → 传 provider，调用成功）
- 副作用：全程无异常外抛

---

#### 用例 U4：`_sig_cache` 命中后不再重复 inspect（含 False 结果命中）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 [P0] + R3 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`_accepts` 的 `cached is not None` 命中分支（True 与 False 两态都验证） |
| Oracle | golden value + 探测计数 |
| Mock | 是（唯一允许）：monkeypatch 包装 `pandaren.hook.hooks.inspect.signature` 做**调用计数**（wraps 原函数，不改行为）——理由：命中数无法从外部观测，必须探测 |

**Given**（前置条件）：
- `composite.add(OldSigHook("old"))`（_accepts → False 态）；`composite.add(NewSigHook("new"))`（→ True 态）
- monkeypatch 包装 `inspect.signature` 并置计数器 `calls = 0`

**When**（操作/动作）：
- ① `composite.on_before_llm_call(messages=[{"role":"user","content":"hi"}], run_id="run-1", model="gpt-4o", tools=None, call_type="main", session_id="sess-42", provider="openai")`
- ② 再次调用相同参数

**Then**（预期结果）：
- 两次调用后 `calls == 2`（old 一次 + new 一次，均只内省 1 次；**第二次调用 0 次新增** → 命中缓存）
- `_sig_cache` 含 key `(id(old_hook), "on_before_llm_call", "provider")` 值为 `False` 的条目（False 也被缓存，非仅 True）
- 副作用：`old.calls` 长度 2，两次均无 provider 键（False 缓存未破坏行为）

---

#### 用例 U5：`clone()` 后 `_hooks` 列表相互独立（add 不影响对方）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 [P0] + R4 |
| 测试层级 | component(fake) |
| 覆盖准则 | 无分支 |
| Oracle | 副作用 oracle（Fake 记录） |
| Mock | 否 |

**等价类划分**：add 操作对象 ∈ {原实例} / {克隆实例}（互不影响两个方向都验证）

**Given**（前置条件）：
- `orig = CompositeAgentHooks()`；`orig.add(RecordingHook("a"))`
- `clone = orig.clone()`

**When**（操作/动作）：
- 方向 1：`orig.add(RecordingHook("b"))` → 调 `clone.on_run_start(task="t", run_id="r", session_id="s")`
- 方向 2：`clone.add(RecordingHook("c"))` → 调 `orig.on_run_start(task="t", run_id="r", session_id="s")`

**Then**（预期结果）：
- 方向 1：`clone` 内 hook 数量 = 1（clone 不受 orig.add 影响）；`clone` 调用后仅 `a.calls` 有记录
- 方向 2：`orig` 内 hook 数量 = 2（orig 不受 clone.add 影响）；`orig` 调用后 `a.calls`、`b.calls` 各 +1，`c.calls` 为空
- 副作用：两实例 `_hooks is not` 同一列表对象

---

#### 用例 U6：`clone()` 元素引用共享 + `_sig_cache` 独立

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 [P0] + R4 |
| 测试层级 | component(fake) |
| 覆盖准则 | 无分支 |
| Oracle | 引用同一性断言（is）+ golden value |
| Mock | 否 |

**Given**（前置条件）：
- `orig = CompositeAgentHooks()`；`hook_a = RecordingHook("a")`；`orig.add(hook_a)`
- 在 `orig` 上触发一次 `_accepts`（如调用 on_before_llm_call 使 `_sig_cache` 非空）
- `clone = orig.clone()`

**When**（操作/动作）：
- ① 断言 `clone._hooks[0] is hook_a`（元素引用共享）
- ② 断言 `clone._sig_cache == {}`（clone 的缓存独立，不复制原缓存）

**Then**（预期结果）：
- ① `clone._hooks[0] is hook_a` 为 True（浅拷贝边界：元素共享）
- ② `clone._sig_cache == {}` 为 True（新实例空缓存）；随后在 `clone` 上触发 on_before_llm_call，`orig._sig_cache` 条目数不变（互不写入）

---

#### 用例 U7：run 级 hook 的 session_id 原样透传给每个子 hook（参数化 17 个 run 级方法）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 [P0] + R5 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：17 个 run 级方法各自的 `session_id=session_id` 转发（全走真分支） |
| Oracle | 副作用 oracle（Fake 记录） |
| Mock | 否 |

**等价类划分**：session_id ∈ {非空 `"sess-42"`}（原样透传，非空才能暴露"丢失/覆盖/替换"类缺陷）；子 hook 数量 = 2（验证"每个"）

**Given**（前置条件）：
- `composite.add(RecordingHook("h1"))`；`composite.add(RecordingHook("h2"))`
- 参数按 §1.3 参数表取 run 级 17 行的值（session_id 统一 `"sess-42"`）

**When**（操作/动作）：
- 对 17 个 run 级方法逐一调用：`getattr(composite, method)(**params)`

**Then**（预期结果）：
- 每个子 hook（h1、h2）的对应记录 `kwargs["session_id"] == "sess-42"`（原样、未丢失、未改写）
- 其余字段与调用值逐键一致（防止"只验证 session_id 忽略其他字段"的盲区）

---

#### 用例 U8：三实现 21 方法集合一致 + run/非 run 级签名约束

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 [P0] + inv-9 [P1] + R6 |
| 测试层级 | unit |
| 覆盖准则 | 无分支（集合对照） |
| Oracle | golden value（21 个名字 + run/非 run 归属，由 hooks.py 注释分区独立推导） |
| Mock | 否 |

**等价类划分**：方法名空间 = 规格中 21 个名字（§1.3 表）；签名维度 = 每个方法签名是否含 `session_id` 参数

**Given**（前置条件）：
- 规格期望集合：21 个方法名（§1.3 表 #1-#21）；run 级 17 个 = 表中 ✅ 行；非 run 级 4 个 = ❌ 行（on_tool_register / on_tool_circuit_open / on_tool_circuit_close / on_tool_output_truncated）

**When**（操作/动作）：
- ① 取 `AgentHooks.__dict__` 中所有 `on_` 开头的方法名集合；② 同法取 DefaultAgentHooks / CompositeAgentHooks；③ 对每类的每个方法用 `inspect.signature` 检查参数名

**Then**（预期结果）：
- 三个集合两两相等，且长度 == 21
- 每类中：17 个 run 级方法签名含 `session_id` 参数；4 个非 run 级签名**不含** `session_id`（三类的签名约束一致）

---

#### 用例 U9：DefaultAgentHooks 实例满足 AgentHooks Protocol

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 [P0] + R7 |
| 测试层级 | unit |
| 覆盖准则 | 无分支 |
| Oracle | golden value（isinstance 结果） |
| Mock | 否 |

**Given**（前置条件）：
- 无（直接构造）

**When**（操作/动作）：
- `isinstance(DefaultAgentHooks(), AgentHooks)`

**Then**（预期结果）：
- 返回 `True`（runtime_checkable Protocol 结构检查通过——Default 方法齐全即满足）

---

#### 用例 U10：hook 按 add 顺序执行

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 [P0] + R8 |
| 测试层级 | component(fake) |
| 覆盖准则 | 无分支 |
| Oracle | 副作用 oracle（调用序列） |
| Mock | 否 |

**等价类划分**：hook 数量 = 3（中位数，能暴露逆序/乱序）；方法代表 = on_run_start（模式 A 代表，其余 20 个同构转发循环）

**Given**（前置条件）：
- `composite.add(RecordingHook("h1"))` → `add("h2")` → `add("h3")`

**When**（操作/动作）：
- `composite.on_run_start(task="task-1", run_id="run-1", session_id="sess-42")`

**Then**（预期结果）：
- 记录到的调用方法顺序 == `["h1", "h2", "h3"]`（每 hook 恰 1 条，先后与 add 顺序一致）
- 断言方式：比较三 hook 各自 `calls` 的最后一条时间序（如统一挂到一个共享 list，或比较调用序号），禁止按 dict 乱序断言

---

#### 用例 U11：非 run 级 hook 不接收 session_id（签名 + 转发行为双验证）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-9 [P1] + R9 |
| 测试层级 | unit（签名） + component(fake)（转发行为） |
| 覆盖准则 | 分支：模式 C 4 个方法的转发路径 |
| Oracle | golden value（签名无 session_id）+ 副作用 oracle（转发 kwargs 无 session_id 键） |
| Mock | 否 |

**等价类划分**：4 个非 run 级方法全量验证（#9/#12/#13/#14）

**Given**（前置条件）：
- 签名断言：对 AgentHooks / DefaultAgentHooks / CompositeAgentHooks 的 `on_tool_register`、`on_tool_circuit_open`、`on_tool_circuit_close`、`on_tool_output_truncated` 做 `inspect.signature` 检查（无 `session_id` 参数）
- 转发断言：`composite.add(RecordingHook("h1"))`

**When**（操作/动作）：
- 按 §1.3 表 #9/#12/#13/#14 参数调用 `composite` 的对应方法（**不带** session_id）

**Then**（预期结果）：
- 四方法的签名均无 `session_id` 参数（三类一致）
- `h1.calls` 中对应记录 kwargs 无 `session_id` 键
- 若调用方误传 `session_id="x"`（对照场景）：参数绑定 `TypeError` 在 Composite 方法入口
  （for/try 之前）抛出并**向上传播**（Python 参数绑定发生在函数体执行前）——调用方错误在
  调用点即暴露，优于被吞掉；测试按实际行为断言，见 §8 KG-3。

---

#### 用例 U12：AgentHooks 是 Protocol，不可直接实例化

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-10 [P1] + R10 |
| 测试层级 | unit |
| 覆盖准则 | 无分支 |
| Oracle | golden value（TypeError 可直断） |
| Mock | 否 |

**Given**（前置条件）：
- 无

**When**（操作/动作）：
- ① `AgentHooks()`；② `issubclass(AgentHooks, Protocol)`；③ 检查 `AgentHooks` 是否带 `@runtime_checkable`（`getattr(AgentHooks, "_is_runtime_protocol", False)` 或 `AgentHooks in Protocol.__runtime_checkable__` 集合）

**Then**（预期结果）：
- ① 抛 `TypeError`（Protocol 无具体实现不可实例化）
- ② 为 `True`（是 Protocol 子类）
- ③ 为 `True`（runtime_checkable 已生效，支撑 U9 的 isinstance 语义）

---

#### 用例 U13：Composite 空列表调用任意 hook 不抛异常、无副作用

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-11 [P1] + R11 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：21 个方法在 `self._hooks` 为空时的 for 循环零次迭代路径 |
| Oracle | 无异常 + 无状态变更 |
| Mock | 否 |

**Given**（前置条件）：
- `composite = CompositeAgentHooks()`（不 add 任何 hook）

**When**（操作/动作）：
- 按 §1.3 参数表对 21 个方法逐一调用（参数化）

**Then**（预期结果）：
- 全部调用不抛异常
- 副作用：`composite._hooks == []`（无隐式状态变更）、`composite._sig_cache == {}`

---

#### 用例 U14：`add(None)` 被忽略，不注入 None 元素

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R12 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`add` 的 `if hooks is not None` 假分支 |
| Oracle | 副作用 oracle（_hooks 长度） |
| Mock | 否 |

**Given**（前置条件）：
- `composite = CompositeAgentHooks()`

**When**（操作/动作）：
- `composite.add(None)`；`composite.add(RecordingHook("h1"))`；`composite.add(None)`

**Then**（预期结果）：
- `composite._hooks == [h1]`（长度 1，两个 None 均被忽略）
- `composite.on_run_start(task="task-1", run_id="run-1", session_id="sess-42")` 不抛异常，`h1.calls` 长度 1

---

#### 用例 U15：参数透传完整性 —— on_before_llm_call 全字段原样透传

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R13 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：模式 B 的 kwargs 组装（model/tools/call_type/session_id 恒传 + provider 条件） |
| Oracle | 参考行为（调用值即期望值，identity 透传） |
| Mock | 否 |

**等价类划分**：字段维度 = {model, tools, call_type, session_id, provider} 全量（防"只测 provider 漏测恒传字段"）；tools ∈ {None, 非空 list}（None 与 list 两态代表）

**Given**（前置条件）：
- `composite.add(NewSigHook("h1"))`

**When**（操作/动作）：
- `composite.on_before_llm_call(messages=[{"role":"user","content":"hi"}], run_id="run-1", model="gpt-4o", tools=[{"function":{"name":"web_search"}}], call_type="search", session_id="sess-42", provider="openai")`

**Then**（预期结果）：
- `h1.calls[-1]["kwargs"] == {"model": "gpt-4o", "tools": [{"function":{"name":"web_search"}}], "call_type": "search", "session_id": "sess-42", "provider": "openai"}`（五字段逐键相等；`tools` 为 list 而非 None）
- 对照：tools=None 子场景 → kwargs["tools"] is None（None 不被改写为空 list）

---

#### 用例 U16：BaseException（KeyboardInterrupt）不被 catch 吞掉，向外传播

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R14 [P3]（设计确认项） |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`except Exception` 不匹配 BaseException 的路径 |
| Oracle | 参考行为（KeyboardInterrupt 应传播） |
| Mock | 否 |

**Given**（前置条件）：
- `composite.add(BaseThrowHook("h1"))`（on_run_start 抛 `KeyboardInterrupt`）；`composite.add(RecordingHook("h2"))`

**When**（操作/动作）：
- `composite.on_run_start(task="task-1", run_id="run-1", session_id="sess-42")`

**Then**（预期结果）：
- 调用向外抛 `KeyboardInterrupt`（不被 `except Exception` 捕获）
- 副作用：`h2.calls` 为空（中断阻止了后续 hook）
- 标注：此为设计取舍的**行为确认**用例（若产品方希望连 BaseException 也吞，需改实现并更新本用例为 [known-gap]）

---

#### 用例 U17：integration —— 真实消费方 ObservabilityHooksAdapter 链式调用 + 缺方法容错

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 [P0] + inv-5 [P0] + R13 [P2]（真实链式回归） |
| 测试层级 | integration |
| 覆盖准则 | 无分支（真实消费方冒烟） |
| Oracle | 副作用 oracle（InMemory backend 记录 + adapter 内部状态） |
| Mock | 否 — 真实 adapter + 内存 backend（InMemoryLoggerBackend / InMemoryTracerBackend / InMemoryMetricsBackend，见 `pandaren/observability/backend/in_memory.py`；Tracer 构造的 backend 注入参数为推断，test-coder 需按实际签名确认） |

**等价类划分**：hook 组合 = builder.py:909-912 的真实形态（adapter 先 add、用户 hook 后 add）；方法代表 = on_run_start（模式 A）/ on_before_llm_call（模式 B）/ on_tool_register（模式 C）/ on_skill_activated（KG-1 缺方法）

**Given**（前置条件）：
- 构造 `Logger(backend=InMemoryLoggerBackend())`、`Tracer(backend=InMemoryTracerBackend())`、`Metrics(backend=InMemoryMetricsBackend())`
- `adapter = ObservabilityHooksAdapter(logger=..., tracer=..., metrics=...)`
- `composite.add(adapter)`；`composite.add(RecordingHook("user"))`（模拟 builder 先 add 底座再 add 用户 hooks）
- 时钟不冻结；断言只查条目存在性与字段，不断言 `duration_ms`/时间值（防 flaky）

**When**（操作/动作）：
- ① `composite.on_run_start(task="task-1", run_id="run-1", session_id="sess-42")`
- ② `composite.on_before_llm_call(messages=[{"role":"user","content":"hi"}], run_id="run-1", model="gpt-4o", tools=None, call_type="main", session_id="sess-42", provider="openai")`
- ③ `composite.on_tool_register(tool_name="web_search", tier="standard", sensitivity="high", namespace=None)`
- ④ `composite.on_skill_activated(skill_name="math", skill_type="ACTION", tools=["calc"], run_id="run-1", step_n=1, session_id="sess-42")`

**Then**（预期结果）：
- ① 不抛异常；`adapter._run_start_mono_by_run` 含 key `"run-1"`；logger backend 记录含 `run_id="run-1"` 与 `session_id="sess-42"`（session_id 真实落观测，验证 inv-5 端到端）
- ② 不抛异常；adapter 内部 `_llm_call_provider_by_run["run-1"] == "openai"`（provider 真实送达消费方，验证 inv-2 端到端）；`user.calls` 中 provider=`"openai"`（用户 hook 同步收到）
- ③ 不抛异常；`user.calls` 有 on_tool_register 记录且 kwargs 无 session_id 键
- ④ **不抛异常**（KG-1：adapter 无 on_skill_activated → AttributeError → Composite 容错吞掉）→ `user.calls` 有 on_skill_activated 记录（后续 hook 未被中断）——同时回归验证 inv-1 在真实消费方缺方法场景下成立

---

## 8. 已知差距（Known-Gap）

| 编号 | 用例 | 期望行为 | 实际现状 | 差距原因 |
|------|------|---------|---------|---------|
| KG-1 | U17 | 委派任务称 ObservabilityHooksAdapter 实现完整 21 个 hook | `hooks_adapter.py` 实际实现 **19/21**（缺 `on_skill_activated` / `on_skill_cleared`），其 docstring 明示"由 SkillManager 直接处理，不桥接" | 委派描述与源码事实不符；adapter 侧为设计取舍非缺陷。U17 按 19 个真实方法 + 2 个缺方法容错场景设计，不要求 adapter 补齐 |
| KG-2 | U1 例外预期 | 非 run 级 4 个方法转发不含 session_id | 与源码一致 | 仅提醒 test-coder 参数化断言时按 §1.3 ❌ 行处理，避免把"无 session_id"误判为缺陷 |
| KG-3 | U11 对照 | 误传 session_id 时 TypeError 被 Composite 自身 catch 吞掉 | 参数绑定 TypeError 在 Composite 方法入口（for/try 之前）抛出并向上传播 | Python 参数绑定发生在函数体执行前，初版预期未考虑绑定时机。外抛传播更优（调用方错误在调用点暴露），测试按实际行为断言（`pytest.raises(TypeError)`），非本期修复目标 |

> 未发现被测文件本身（hooks.py）的设计-实现不一致。若 U16 的 BaseException 传播行为经产品确认"应吞掉"，将改设计并在此登记。

## 9. 修订记录

| 版本 | 日期 | 变更 | 理由 |
|------|------|------|------|
| v1 | 初始 | 建立全部用例 | — |
