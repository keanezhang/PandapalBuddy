# SessionAgentPool 会话模式锁定 测试设计

- 被测目标：`pandapal/scheduler/agent_pool.py` 的 `SessionAgentPool._get_or_materialize` 与 `_apply_mode`
- 测试栈：Python / pytest（`@pytest.mark.asyncio`）
- 测试桩：复用 `pandapal/scheduler/tests/test_agent_pool.py` 的 `FakeBlueprint` / `FakeBroadcast` 模式，并扩展 `FakeAgent` 支持 `rebind_system_prompt` 调用记录；内容刷新用例复用 `pandapal/local/tests/test_prompt_fragments.py` 的 `PromptAssembler + tmp_path` 模式。

---

## 一、风险 / 不变式清单（按优先级排序）

| 编号 | 类型 | 表述 | 优先级 |
|------|------|------|:--:|
| inv-1 | 不变式 | 会话级模式锁定：`bound_mode` 一旦在首次 materialize 时非 None 绑定，后续「已有 entry 复用」与「竞态复用」分支均传 `None`，绝不随消息 `mode` 改变 | P0 |
| inv-2 | 不变式 | resume 缺省（`mode=None`，如 HITL / ask_user / Plan 恢复、企微 / 小智渠道）沿用 `entry.bound_mode`，**绝不回退 `default_mode`** | P0 |
| inv-3 | 不变式 | 首次绑定正确性：新造分支按消息 `mode` 锁定；首次 `mode=None` 时落 `default_mode`；`bound_prompt` 与 `bound_mode` 同步更新 | P1 |
| inv-4 | 不变式 | 内容刷新不被锁定误伤：`mode` 不变但 `_resolve_prompt` 内容变化（工作区片段文件变更）时，仍触发 rebind | P1 |
| inv-5 | 不变式 | 非法 / 未知 `mode` 安全：`_apply_mode` 早退，不 crash、不误 rebind，保持当前绑定 | P1 |
| inv-6 | 不变式 | 竞态复用锁定：并发 materialize 同一 session 时，后到者复用 `existing`，传 `None`，沿用先到者锁定，弃用自身新造实例 | P2 |
| inv-7 | 不变式 | rebind 失败保底：`agent.rebind_system_prompt` 抛异常时不传播，保持旧 `bound_mode` / `bound_prompt` | P2 |

对应风险点（RISK 维度）：

| 风险 | 表述 | 严重度 | 可能性 | 优先级 |
|------|------|:--:|:--:|:--:|
| R-1 | 消息携带不同 `mode` 导致会话中途切换（锁定失效） | 中 | 高 | P0 |
| R-2 | resume / 非桌面渠道缺省 `mode` 回退 default，破坏会话上下文 | 中 | 高 | P0 |
| R-3 | 工作区片段文件变更被 delta 跳过，prompt 过期 | 中 | 中 | P1 |
| R-4 | 未知 `mode` 触发异常 / 误 rebind | 中 | 中 | P1 |
| R-5 | 竞态下复用分支误用本消息 `mode` | 中 | 低 | P2 |
| R-6 | rebind 抛异常导致 `acquire` 整体失败 | 中 | 低 | P2 |

---

## 二、覆盖准则声明

- `_get_or_materialize` 三分支全覆盖：①已有 entry 复用 ②新造 entry ③竞态复用 existing。
- `_apply_mode` 四路径全覆盖：①`prompt is None` 早退 ②`bound_prompt == prompt` 内容相同跳过 ③内容不同 → rebind ④rebind 抛异常 → except 保底。
- 目标达到 **branch coverage（分支覆盖，默认目标）**。本对象无复合布尔判定，无需 MC/DC。

---

## 三、Mock / Fake 策略决策

| 依赖 | 决策 | 理由 |
|------|------|------|
| `AgentBlueprint` | **Fake**（`FakeBlueprint`） | `materialize()` 只需返回可记录 rebind 的 Agent 桩；竞态用例需要可控阻塞。无真实 SDK 组装。 |
| `Agent` | **Fake**（扩展 `FakeAgent`） | 需要记录 `rebind_system_prompt` 调用序列与次数；`cancel` / `aclose` 保留供 `stop()` 清理。 |
| `MessageBroadcast` | **Fake**（`FakeBroadcast`） | 本改动不涉及广播语义，仅需满足构造校验并静默收集事件。 |
| `prompt_by_mode` | **真实现（静态 dict）** | 静态路径下 prompt 值白纸黑字可独立推导，是最强的 golden oracle。用于 component 层用例。 |
| `PromptAssembler` | **真实现（真实 tmp_path 文件）** | 内容刷新是待证链路，工作区片段文件变更必须走真实文件系统，故用例 4 为 integration 层。 |
| `_get_or_materialize` 内部状态 | **白盒访问 `pool._agents[session_id]`** | `bound_mode` / `bound_prompt` 是 `_SessionAgentEntry` 内部字段，直接断言是证明锁定的最强断言。 |

---

## 四、等价类划分总览

对 `_apply_mode(entry, mode)` 输入空间：

| 维度 | 等价类 | 代表值 |
|------|--------|--------|
| `mode` 取值 | 已配置合法 mode | `"coding"` / `"office"` |
| | 缺省（resume / 非桌面渠道） | `None` |
| | 非法 / 未知 | `"bogus"`（空串 `""` 同属非法早退类，由 `"bogus"` 代表） |
| 会话阶段 | 首次 materialize（新造分支，直接传 `mode`） | 首次 `acquire` |
| | 后续复用（复用分支，强制传 `None`） | 第二次 `acquire` |
| | 并发竞态复用（竞态分支，强制传 `None`） | 两协程同时首次 `_get_or_materialize` |
| `bound_prompt` 内容关系 | 内容相同 → 跳过 | 复用 / resume 且片段未变 |
| | 内容不同 → rebind | 片段文件变更 |
| | `prompt is None` → 早退 | 非法 / 未知 mode |

---

## 五、用例 × 风险覆盖矩阵

| 用例 | inv-1 锁定 | inv-2 resume | inv-3 首次绑定 | inv-4 内容刷新 | inv-5 非法 mode | inv-6 竞态 | inv-7 rebind 保底 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| 1. 首次 coding 锁定 + 后续 office 不切换 | ✅ | | ✅(首次) | | | | |
| 2. resume mode=None 保持绑定 | ✅ | ✅ | | | | | |
| 3. 新会话首次 mode=None 落 default | | ✅ | ✅ | | | | |
| 4. 内容刷新仍触发 rebind | | | | ✅ | | | |
| 5. 非法 mode 早退保持绑定 | | | | | ✅ | | |
| 6. 竞态复用 existing 同样锁定 | ✅ | | | | | ✅ | |
| 7. rebind 抛异常保持旧绑定 | | | | | | | ✅ |

---

## 六、用例设计

> 统一桩约定：`FakeAgent(tag)` 含 `cancel()` / `aclose()`，并新增 `rebind_system_prompt(prompt)` 把 `prompt` append 到 `self.prompts` 列表（初始 `[]`）。`FakeBlueprint.materialize()` 返回自增 tag 的 `FakeAgent`。`FakeBroadcast.send()` 静默收集。
>
> 桩须如实携带 blueprint 的 `system_prompt`（= materialize 出的 Agent Memory **已烤入**的 prompt，生产见 run_local: `agent_builder.system_prompt(assembler.get(DEFAULT_MODE))`）。`FakeBlueprint` 增加 `system_prompt` 属性，`_make_pool` 按 `default_mode` 填入烤入值。`agent_pool` 新造 entry 的初始 `bound_prompt` **取自该烤入值（唯一真相源）**，而非此刻重新解析的值——否则片段文件在 blueprint 构建后出现时，判据会与实际 prompt 漂移而漏掉 rebind（回归用例 `test_fragment_added_after_blueprint_build_is_bound` 覆盖此坑）。

---

#### 用例1：会话首次 coding 锁定后，后续 office 不再切换

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 锁定 [P0] + inv-3 首次绑定 [P1] + R-1 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_get_or_materialize`「已有 entry 复用」分支 + `_apply_mode`「`bound_prompt == prompt` 跳过」分支 |
| Oracle | golden value（`bound_mode`/`bound_prompt` 由静态 dict 独立推导；rebind 记录精确断言） |
| Mock | 是 — `FakeBlueprint`/`FakeAgent`/`FakeBroadcast` 均为内存桩，无真实 I/O |

**等价类划分**：mode ∈ {合法 coding, 合法 office} × 会话阶段 {首次, 后续} → 代表值：首次 `"coding"`，后续 `"office"`。

**Given**：
- `prompt_by_mode = {"coding": "CODING_PROMPT", "office": "OFFICE_PROMPT"}`，`default_mode = "office"`。
- 构造 `pool` 并 `await pool.start()`。
- `FakeAgent.prompts` 初始为 `[]`。

**When**：
- 第一次：`async with pool.acquire("s1", "u", mode="coding") as a1: pass`（首次 materialize，锁定 coding）。
- 记录 `entry = pool._agents["s1"]`。
- 第二次：`async with pool.acquire("s1", "u", mode="office") as a2: pass`（复用，忽略 office）。

**Then**：
- 返回值：`a2 is a1`（同 session 复用同一 Agent 实例）。
- `entry.bound_mode == "coding"`（未被 office 改变）。
- `entry.bound_prompt == "CODING_PROMPT"`。
- 副作用：`a1.prompts == ["CODING_PROMPT"]`（rebind 仅首次 1 次；第二次 office 未触发第二次 rebind）。
- 清理：`await pool.stop()`。

---

#### 用例2：resume（mode=None）保持绑定，绝不回退 default_mode

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 resume [P0] + inv-1 锁定 [P0] + R-2 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：复用分支 + 跳过分支；`mode=None → target_mode=entry.bound_mode` 路径 |
| Oracle | golden value |
| Mock | 是 — 内存桩 |

**等价类划分**：resume 等价类 `mode=None`（HITL / ask_user / Plan 恢复、企微 / 小智渠道）→ 代表值：先绑定 `"coding"`，后 `None`。

**Given**：
- `prompt_by_mode = {"coding": "CODING_PROMPT", "office": "OFFICE_PROMPT"}`，`default_mode = "office"`。
- `await pool.start()`。
- 已执行一次 `async with pool.acquire("s1", "u", mode="coding"): pass`，锁定 coding。

**When**：
- `async with pool.acquire("s1", "u", mode=None): pass`（resume，缺省 mode）。

**Then**：
- `pool._agents["s1"].bound_mode == "coding"`（保持，绝不回退 office）。
- `pool._agents["s1"].bound_prompt == "CODING_PROMPT"`。
- 副作用：`agent.prompts == ["CODING_PROMPT"]`（无新增 rebind，因内容未变）。
- 清理：`await pool.stop()`。

---

#### 用例3：新会话首次 mode=None 落 default_mode

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 首次绑定 [P1] + inv-2 的 baseline（非桌面渠道）[P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：新造分支 + 跳过分支（`mode=None → target=bound_mode=default`，`prompt == bound_prompt` 跳过） |
| Oracle | golden value |
| Mock | 是 — 内存桩 |

**等价类划分**：首次 + `mode=None`（企微 / 小智 / 非桌面渠道首条消息）→ 代表值：`default_mode="office"`，首次 `mode=None`。

**Given**：
- `prompt_by_mode = {"coding": "CODING_PROMPT", "office": "OFFICE_PROMPT"}`，`default_mode = "office"`。
- `await pool.start()`，`FakeAgent.prompts` 初始 `[]`。

**When**：
- `async with pool.acquire("s1", "u", mode=None): pass`。

**Then**：
- `pool._agents["s1"].bound_mode == "office"`（落 default）。
- `pool._agents["s1"].bound_prompt == "OFFICE_PROMPT"`。
- 副作用：`agent.prompts == []`（初始绑定已 = default prompt，首个 run 内容一致即跳过，无多余 rebind）。
- 清理：`await pool.stop()`。

---

#### 用例4：同 mode 但工作区片段内容变更，仍触发 rebind（内容刷新不被锁定误伤）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 内容刷新 [P1] + R-3 [P1] |
| 测试层级 | integration（真实文件系统 `tmp_path`，`PromptAssembler` 读真实文件） |
| 覆盖准则 | branch：复用分支 + `_apply_mode`「内容不同 → rebind」分支 |
| Oracle | golden value（V1/V2 片段内容可独立推导，断言新 prompt 含 V2 不含 V1） |
| Mock | 部分 — `FakeBlueprint`/`FakeBroadcast` 为桩；`PromptAssembler` 必须为真实现（工作区热重载是待证链路） |

**等价类划分**：内容状态 {不变→跳过, 变更→rebind}，本次取「变更」；mode 取与锁定相同 `"coding"`。代表值：`PANDAPAL.md` 内容 `"FRAGMENT-V1"` → `"FRAGMENT-V2"`。

**Given**：
- `tmp_path / "PANDAPAL.md"` 写入 `"FRAGMENT-V1"`。
- 构造 `PromptAssembler(base_prompts={"coding": "CODING_BASE", "office": "OFFICE_BASE"}, env_block="## ENV\nws=/tmp", work_dir=tmp_path)`。
- 构造 `pool = SessionAgentPool(..., prompt_assembler=assembler, default_mode="coding")`，`await pool.start()`。
- 首次 `async with pool.acquire("s1", "u", mode="coding"): pass` 锁定 coding；此时 `agent.prompts == []`（初始 `bound_prompt` 已 = coding prompt，跳过）。

**When**：
- 修改 `tmp_path / "PANDAPAL.md"` 内容为 `"FRAGMENT-V2"`。
- `async with pool.acquire("s1", "u", mode="coding"): pass`（同 mode，但片段内容已变）。

**Then**：
- 副作用：`agent.prompts` 长度 == `1`（触发一次 rebind）。
- `agent.prompts[-1]` 含 `"FRAGMENT-V2"` 且不含 `"FRAGMENT-V1"`。
- `pool._agents["s1"].bound_mode == "coding"`（模式未被刷新误伤/改变）。
- `pool._agents["s1"].bound_prompt == agent.prompts[-1]`（已更新为新 prompt）。
- 清理：`await pool.stop()`。

---

#### 用例5：非法 / 未知 mode 早退，保持当前绑定，不 crash、不误 rebind

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 非法 mode [P1] + R-4 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：新造分支 + `_apply_mode`「`prompt is None` → return」早退分支 |
| Oracle | golden value |
| Mock | 是 — 内存桩 |

**等价类划分**：非法 mode ∈ {未知字符串 `"bogus"`, 空串 `""`}（`""` 经 `_resolve_prompt` 的 `if not mode` 同样早退，由 `"bogus"` 代表）。代表值：`mode="bogus"`。

**Given**：
- `prompt_by_mode = {"coding": "CODING_PROMPT", "office": "OFFICE_PROMPT"}`，`default_mode = "office"`。
- `await pool.start()`，`FakeAgent.prompts` 初始 `[]`。

**When**：
- `async with pool.acquire("s1", "u", mode="bogus"): pass`。

**Then**：
- 不抛异常（不 crash）。
- `pool._agents["s1"].bound_mode == "office"`（保持 default，未误绑 bogus）。
- `pool._agents["s1"].bound_prompt == "OFFICE_PROMPT"`。
- 副作用：`agent.prompts == []`（无 rebind）。
- 清理：`await pool.stop()`。

---

#### 用例6：并发竞态下复用 existing entry 时同样锁定（不随本消息 mode 切换）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 竞态复用 [P2] + inv-1 锁定 [P0] + R-5 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_get_or_materialize`「竞态复用 existing」分支 + `_apply_mode(existing, None)` 跳过分支 |
| Oracle | golden value（`bound_mode` 由先到者 coding 决定，可独立推导） |
| Mock | 是 — `FakeBlueprint.materialize` 可控阻塞制造竞态；`FakeAgent` 记录 rebind 与 `aclose` |

**等价类划分**：并发时序 {两协程同时首次 materialize 同一 session}，mode 组合 {先到者 `"coding"`, 后到者 `"office"`}。代表值：A 先 materialize 并先创建 entry（coding），B 后到复用 existing（office 应被忽略）。

**Given**：
- 直接调用 `pool._get_or_materialize("s1", "u", mode)`（**绕过 `acquire` 的 per-session lock**，否则同 session 会串行、竞态分支不可达）。
- `prompt_by_mode = {"coding": "CODING_PROMPT", "office": "OFFICE_PROMPT"}`，`default_mode = "office"`。
- `FakeBlueprint.materialize` 可控阻塞：第一次调用（协程 A）与第二次调用（协程 B）都进入 materialize 并等待同一 `release`；`release` 后保证 A 先完成返回、先持有 `_pool_lock` 创建 entry，B 随后返回并发现 `existing`（固定竞态顺序）。

**When**：
- `task_a = asyncio.create_task(pool._get_or_materialize("s1", "u", "coding"))`
- `task_b = asyncio.create_task(pool._get_or_materialize("s1", "u", "office"))`
- 等待 A、B 均进入 materialize → 触发 `release` → `await asyncio.gather(task_a, task_b)` → `await asyncio.sleep(0)`（推进弃用实例的 `aclose` task）。

**Then**：
- `task_a` 返回 `agent_a`（A materialize 的实例）。
- `task_b` 返回的对象 `is agent_a`（`existing.agent`，**不是** B materialize 的 `agent_b`）。
- `pool._agents["s1"].bound_mode == "coding"`（先到者锁定，未被 B 的 office 切换）。
- `pool._agents["s1"].bound_prompt == "CODING_PROMPT"`。
- 副作用：`agent_a.prompts == ["CODING_PROMPT"]`（仅 A 的 `_apply_mode(entry, "coding")` 触发一次；B 复用分支 `_apply_mode(existing, None)` 因内容相同跳过）。
- 弃用清理：`agent_b.closed == True`（竞态弃用实例被 `aclose`，验证「弃用新造」分支）。

---

#### 用例7：rebind 抛异常 → 保持旧绑定，不 crash（异常路径保底）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 rebind 保底 [P2] + R-6 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | branch：`_apply_mode`「内容不同 → rebind」分支 + `try/except` 异常分支 |
| Oracle | golden value（旧绑定不变） |
| Mock | 是 — `FakeAgent.rebind_system_prompt` 注入一次 `RuntimeError`（依赖失败，覆盖 except 分支） |

**等价类划分**：异常路径 {`agent.rebind_system_prompt` 抛异常}，注入点 {`_apply_mode` 中 `entry.agent.rebind_system_prompt(prompt)`}。代表值：mode 切换时 rebind 抛 `RuntimeError`。

**Given**：
- `prompt_by_mode = {"coding": "CODING_PROMPT", "office": "OFFICE_PROMPT"}`，`default_mode = "office"`。
- `await pool.start()`。
- 将 `FakeAgent.rebind_system_prompt` 配置为首次调用抛 `RuntimeError("rebind boom")`。

**When**：
- `async with pool.acquire("s1", "u", mode="coding"): pass`（触发 default office → coding 切换 → rebind → 抛异常）。

**Then**：
- `acquire` 不抛异常（异常被 `_apply_mode` 的 `try/except` 捕获，不向上传播）。
- `pool._agents["s1"].bound_mode == "office"`（保持旧绑定，未被更新为 coding）。
- `pool._agents["s1"].bound_prompt == "OFFICE_PROMPT"`（保持旧 prompt）。
- 副作用：`FakeAgent` 记录了一次 rebind 尝试但抛异常（Agent 内部未成功替换 prompt）。
- 清理：`await pool.stop()`。

---

## 七、Known-Gap 清单

无。改动已落地，源码行为与上述期望一致，无需 `[known-gap]` / `xfail` 标记。

---

## 八、确定性控制（防 flaky）

| 不确定源 | 对策（已写入 Given） |
|---------|---------------------|
| 并发竞态顺序 | 用例 6 通过 `FakeBlueprint.materialize` 可控阻塞 + 固定 A 先返回，钉死「A 先创建 entry、B 后复用」 |
| 时间 / 时钟 | 本设计不依赖 `time.monotonic` 具体值（TTL 未触发），无需 freeze |
| 文件系统 | 用例 4 使用 `tmp_path` 独立临时目录，写入内容固定 V1/V2 |
| 事件循环推进 | 用例 6 弃用实例 `aclose` 由 `asyncio.create_task` 触发，Then 中显式 `await asyncio.sleep(0)` 推进后断言 |
| 集合/序列 | rebind 记录使用有序 `list`，断言精确序列与长度，不做无序比较 |
