# pandaren/plan — Plan Mode：规划-审批-执行（只读探索闭环）

> 模块：pandaren/plan | 生成：2026-08-19 @ git `c7d5e9f`（Initial commit，模块入库后无独立演进）| 锚点以生成时点代码为准

---

## 1. 模块定位与职责（存在的意义）

**为什么存在**：让 Agent 在动手前先进入「只读规划 → 用户审批 → 再实施」的受控闭环——复杂任务不先探索就直接写代码是最大的返工来源。Plan Mode 用**工具暴露面收窄（只读）+ 状态机（planning/executing 二相）+ 用户三向决策（批准/完善/放弃）**把这一流程做成 SDK 内置能力，而非靠提示词约定。

**不建它会怎样**：
- 规划与否全凭 LLM 自觉（提示词提醒），没有机制保证——写文件工具照样暴露，LLM 规划到一半就可能开始改代码；
- 计划文件路径/内容没有统一校验，路径穿越、非法后缀、空计划都能溜进审批；
- 「计划提交后等待审批」的 run 终止、恢复、session_meta 落库没有统一出口，跨 run 恢复无从谈起。

**角色分工图**：

```
┌─────────────────────────────────────────────────────────────────────┐
│  builder.plan_mode(plan_dir=...) → PlanToolFactory（builder.py:932） │
│      └─ 默认装配，Phase 1 注册 enter/write/exit 三个内置工具          │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  pandaren/plan/（本模块，四文件各司其职）                             │
│  tools.py   — 3 个内置工具 executor（B2 纯信号，零副作用）            │
│  files.py   — 文件 IO 纯函数（路径生成/校验/读写/初始写入）            │
│  manager.py — PlanManager 状态机（唯一状态 Owner）                   │
│  prompt.py  — 8 个提示词常量（模板 + 5 类 Reminder）                 │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼ 唯一消费方
┌─────────────────────────────────────────────────────────────────────┐
│  engine/run_core.py（PlanManager 通过公开接口交互）                   │
│    enter() / exit() / reenter() / get_reminder() / filter_tools()    │
│    increment_turn() / handle_tool_result() / _handle_plan_action()   │
│    副作用统一出口：session_meta 写入 + PLAN_APPROVAL_REQUESTED 事件   │
│                      + run 终止（plan_complete 分支）                │
└─────────────────────────────────────────────────────────────────────┘
        ▲ 旁路消费
   memory/reinject/sources.py:346 PlanStateSource（⚠️ key 漂移，见 §6）
   tool/builtin/plan.py          薄工厂（委托 build_plan_mode_tools）
   pandapal/scheduler/plan_manager.py（应用层审批，跨层一笔带过）
```

**覆盖文件清单**（源码 4 个，**测试 0 个**）：

| 文件 | 大小 | 职责 |
|------|------|------|
| `tools.py` | 14.1KB / 370 行 | enter_plan_mode / write_plan / exit_plan_mode 三工具定义 + 构建函数 |
| `manager.py` | 12.0KB / 286 行 | PlanManager 状态机：phase / turns / reminder / 工具过滤 / 跨 run 恢复 |
| `files.py` | 5.2KB / 175 行 | 路径生成（slug 安全化）、路径校验（防穿越）、读写、初始内容写入 |
| `prompt.py` | 20.8KB / 436 行 | 8 个提示词常量（ENTER_DESCRIPTION / PLAN_TEMPLATE / FULL / SPARSE / REFINE / ABANDON / CONTEXT）|

---

## 2. 方案总览（产品视角）

> 非技术读者可只读本节。

### 2a. 在什么场景下解决什么问题（场景穷举）

| 场景 | 已有/缺失 | 该场景下的问题 |
|------|----------|---------------|
| 复杂任务先探索再动手 | 已有 | enter_plan_mode 收窄工具面：只读工具 + write_plan + ask_user |
| 计划落盘（可审阅/可追溯） | 已有 | 计划文件唯一可写文件，write_plan 全量覆盖，3000 行硬限制 |
| 计划提交等待人工审批 | 已有 | exit_plan_mode → PLAN_APPROVAL_REQUESTED 事件 → run 终止 |
| 用户三向决策 | 已有 | approve（批准实施）/ refine（必须带具体指令，否则 fail-fast）/ abandon（清理退出）|
| 长时间规划后 LLM 忘记规矩 | 已有 | Reminder 注入：首轮 FULL → 每 5 轮 SPARSE → re-entry REFINE |
| 跨 run 恢复（审批后 resume） | 已有 | session_meta 三键（phase/file_path/submitted_at）+ restore_from_session_meta |
| 用户自选规划方法论 | 已有 | methodology 参数，优先级高于 FULL reminder |
| 用户对话中指定计划文件路径 | 已有 | plan_file_path 参数优先于 plan_name（tools.py:67-75）|
| 压缩后仍记住当前 plan | 已有 | PlanStateSource 按 `plan_file_path` key 回注 plan 文件（key 漂移已于 2025 修复，见 §6 P1）|
| 规划/审批状态机自动化测试 | 已有 | 55 用例已落地（plan/tests + engine/tests + memory/tests，见 §6 P2）|

### 2b. 总体方案思路（策略）

1. **工具只发信号，内核做副作用**（B2 铁律）：三个工具 executor 只做「校验 + 文件 IO + 返回 `ToolResult(plan_path/data)`」，**不碰** PlanManager 状态、不写 session_meta、不发事件。状态迁移由 run_core 检测工具名 + success 后统一驱动——LLM 视角是「调工具」，系统视角是「信号触发状态机」。
2. **单一状态 Owner**：PlanManager 是唯一持有 planning 状态的对象，run_core 只经公开接口（enter/exit/reenter/filter_tools/get_reminder…）交互；文件侧真相是「磁盘 `plan_exists()` + `session_meta["plan_submitted_at"]`」，不追踪 `_plan_written` 类冗余状态（manager.py:8, 29-30 自述）。
3. **决策即 fail-fast**：用户三向决策里，refine 不带具体修改指令直接 `raise ValueError`（run_core.py:3266）——禁止 LLM 猜测用户意图；approve 后注入 PLAN_CONTEXT_REMINDER 指引实施；abandon 注入 ABANDON_REMINDER 并清理全部 session_meta。
4. **提交即终止 run**：exit_plan_mode 成功后走 plan_complete 分支——先原子提交记账/审计/step 收尾，再 yield `PLAN_APPROVAL_REQUESTED`（顺序敏感，见 §4 数据流）。

---

## 3. 架构与核心机制

### 3.1 三个内置工具（tools.py）

| 工具 | 输入 | 输出信号 | 校验要点 |
|------|------|---------|---------|
| `enter_plan_mode` | plan_name? / plan_file_path? / (plan_dir 闭包) | ToolResult(success, data=引导文本, **plan_path**) | 路径优先级：plan_file_path > plan_name > 自动生成（`{sid[:12]}-{rid[:8]}`）|
| `write_plan` | content / plan_file_path | ToolResult(success, plan_path, data={line_count, exceeds_limit, message}) | content 非空；>3000 行返回警告文案；**不校验 plan_file_path 合法性**（仅非空）|
| `exit_plan_mode` | plan_file_path | ToolResult(success, plan_path, data={plan_path, plan_content, message}) | 文件必须存在且非空；读取全文回传 |

要点：
- **enter 的 guidance 不含 Phase 方法论**——详细方法论下一轮以 FULL_PLANNING_REMINDER 注入（tools.py:91-92 注释自述），避免重复塞上下文。
- **write/exit 的 plan_file_path 由 LLM 原样传回**（enter 返回的路径），B2 设计下工具间通过参数接力，不经 PlanManager。
- 三个工具 policy 均为 `read_only=False` 但 `is_reversible=True, is_idempotent=True`；exit 额外 `audit_required=True`（提交审批是审计点）。
- `PLAN_MODE_ALLOWED_BUILTIN = {enter, write, exit, ask_user}`（tools.py:43）——过滤白名单核心。

### 3.2 PlanManager 状态机（manager.py）

**二相状态**：`_plan_phase ∈ {"planning", "executing"}`；默认 executing（未启用 Plan Mode 时 PlanManager 存在但 inert）。

**状态迁移**：

```
                 enter()                    exit_plan_mode 成功
  executing ───────────────► planning ─────────────────────────► (等待审批, run 终止)
      ▲                          │  ▲                              │
      │        reenter()         │  │         _handle_plan_action   │
      └──────────────────────────┘  └──────────────┬───────────────┘
      approve → exit(True) + context_reminder(PLAN_CONTEXT_REMINDER) → executing
      refine  → reenter() + context_reminder(refine_reminder)       → planning
      abandon → exit(False) + context_reminder(ABANDON_REMINDER)    → executing
```

**Reminder 注入优先级**（`get_reminder()`，manager.py:189-225，每轮构建 LLM 请求前调用）：

1. **re-entry**（`is_reentry=True`）→ PLAN_MODE_REFINE_REMINDER（最高优先，一次性消费后复位）
2. **turn 0**：有 methodology → 用户方法论；否则 FULL_PLANNING_REMINDER
3. **每 5 轮**（`turns_since_reminder >= TURNS_BETWEEN_REMINDERS`）→ SPARSE_PLANNING_REMINDER
4. 其余轮次 → 空串（不注入）

**工具过滤**（`filter_tools()`，manager.py:231-259，与 get_reminder 同时机调用）三条规则：
1. 名字在 `PLAN_MODE_ALLOWED_BUILTIN` → 保留
2. `tool.policy.read_only == True` → 保留（只读探索）
3. 其余（write/edit/delete/bash 写类）→ 过滤掉

**跨 run 恢复**（`restore_from_session_meta()`，manager.py:172-183）只恢复路径与阶段，轮次清零；不追踪冗余状态。

### 3.3 文件层（files.py）

- `generate_word_slug`：中文/英文混合名 → 安全 slug（正则清洗 + 空格转连字符 + max_words 截断）。
- `generate_plan_file_path`：路径优先级 `plan_dir > config_home/plans > {cwd}/.pandaren/plans`；`plan_name.replace("/", "_")` 防穿越；mkdir 自动建目录。
- `validate_plan_file_path`：校验绝对路径、`.md` 后缀、父目录存在、原始字符串含 `..` 即拒（防穿越）。
- `write_initial_plan_file`：enter 时把用户原始需求写入计划文件开头，作为起点（后续 write_plan 全量覆盖）。
- `plan_exists`：存在且内容非空；读写异常返回 False（不抛）。

### 3.4 提示词层（prompt.py，8 常量）

| 常量 | 用途 |
|------|------|
| `ENTER_PLAN_MODE_DESCRIPTION` | enter 工具描述（含"何时调用/何时不调用"门控）|
| `PLAN_TEMPLATE` | write_plan 工具描述内嵌的计划文件格式要求（硬性章节结构）|
| `FULL_PLANNING_REMINDER` | 首轮完整规划方法论（6-Phase: Interview→Explore→Design→Review→Write→Submit）|
| `SPARSE_PLANNING_REMINDER` | 每 5 轮轻量提醒 |
| `PLAN_MODE_REFINE_REMINDER` | re-entry 完善模式指令 |
| `ABANDON_REMINDER` | 放弃后的普通模式回位通知 |
| `PLAN_CONTEXT_REMINDER` | 批准后实施指引（含 `{plan_file_path}` 占位符）|

---

## 4. 数据流链路

### 链路 A：进入规划（LLM 主动）

```
LLM 调 enter_plan_mode ──► executor 确定路径 + 返回引导文本(plan_path)
  ──► run_core 检测 (tc_name=="enter_plan_mode" && success && !is_planning)  [run_core.py:2490]
  ──► PlanManager.enter(file_path)                       # 状态 → planning
  ──► write_initial_plan_file(path, task)                # 写入用户原始需求
  ──► session_meta 写入 4 键（plan_phase=planning / plan_file_path / plan_submitted_at=None / plan_summary=None）
```

### 链路 B：提交审批（write → exit）

```
LLM write_plan(content) ──► 全量覆盖计划文件 ──► LLM exit_plan_mode(path)
  ──► run_core 检测 exit 成功（无条件触发，不要求 is_planning()，边缘防御）[run_core.py:2524]
  ──► PlanManager.handle_tool_result() 消费信号 → 构建"等待批准"消息回写历史 [manager.py:158]
  ──► 原子提交本 step 全部 tool 结果（_commit_tool_step_atomically）
  ──► plan_complete 分支 [run_core.py:2635]：
       ├─ session_meta: plan_submitted_at=now / plan_summary={title, content_hash[:16]}
       ├─ audit RUN_FINISHED(terminal_reason=PLAN_COMPLETE) + hook on_step_end
       ├─ yield PLAN_APPROVAL_REQUESTED(plan_path, plan_content)  ← 消费方见事件即 return+aclose()
       └─ run 终止（记账/审计已在 yield 之前完成——历史 bug 教训，见 §5）
```

### 链路 C：用户决策（resume run）

```
用户 approve/refine/abandon（带 plan_action 参数 resume）
  ──► restore_from_session_meta 恢复 PlanManager
  ──► _handle_plan_action() [run_core.py:3225]
       approve: exit(True) + 用户编辑内容写回文件(静默容错) + context_reminder(PLAN_CONTEXT) + 清 session_meta
       refine : 空 message 直接 raise ValueError（fail-fast）→ reenter() + plan_submitted_at=None + context_reminder(具体指令)
       abandon: exit(False) + 清 session_meta + context_reminder(ABANDON_REMINDER)
  ──► 后续轮次：planning → filter_tools 收窄；executing → context_reminder 注入实施指引
```

### 链路 D：压缩回注（旁路，已修复）

```
memory 压缩 → PostCompactReinjector → PlanStateSource.collect()
  ──► 读 session_meta["plan_file_path"]（META_KEY）→ 读文件 → 截断 → attachment
  ✓ 该 key 与 run_core 写入一致（2025 修复：META_KEY 由 "plan_wip_path" 统一为 "plan_file_path"）
    回归锚：memory/tests/test_plan_state_source.py 字面量断言 + 10 用例
```

---

## 5. 关键设计决策与权衡

| # | 决策 | 理由 | 代价/边界 |
|---|------|------|----------|
| 1 | 工具零副作用（B2 纯信号） | 状态迁移单一出口在 run_core，可测、可审计、可恢复 | LLM 拿到的 write/exit 结果不含 PlanManager 状态，回执全靠 message 文案 |
| 2 | 文件真相 + session_meta 而非内存冗余 | 避免 `_plan_written/_plan_submitted` 类双写漂移（manager.py 自述）| 磁盘 IO 每次轮询（plan_exists）|
| 3 | exit 提交意图不要求 is_planning() | 边缘情况 phase 可能被提前重置，LLM 显式调用即提交意图（run_core.py:2521-2523 注释）| 无状态校验的宽进 |
| 4 | refine 必须带具体修改指令，否则 raise | 禁止 LLM 猜测用户意图，宁可 fail-fast | 应用层须把 ValueError 转成用户可读错误（O3 保证不外抛）|
| 5 | 记账/审计在 yield 审批事件**之前** | 消费方见事件即 return + stream.aclose() → 之后语句永不执行，否则记账丢失/审计漏写（历史 bug，run_core.py:2646-2648 注释）| 顺序耦合：改动事件消费方行为会破坏此假设 |
| 6 | plan_summary 只存 `content_hash[:16]` 截断 | 足够校验计划是否变更，不存全量 | 截断 hash 碰撞概率极低但非零（P3 可忽略）|
| 7 | 路径三优先级（用户路径 > 名称 > 自动） | 尊重用户显式指定，LLM 名称兜底，自动名兜底 | 自动名依赖 session_id/run_id 截断（12/8），多 run 同名覆盖风险低 |

---

## 6. 失败模式与风险（按严重度）

**解决状态总览（2025-06 更新）**：

| 风险 | 状态 | 说明 |
|------|------|------|
| P1 回注 key 漂移（静默失效） | ✅ 已修复 | `META_KEY → "plan_file_path"` + 10 用例回归锚（§6 P1）|
| P2 模块零自动化测试 | ✅ 已补齐 | 55 用例 + 1 xfail 落地三层（§6 P2）|
| P2 approve 静默吞 OSError | ✅ 已修复 | catch-log 留痕 + A3 monkeypatch 故障注入（§6 P2）|
| P3-1 类级可变字段反模式 | ✅ 已修复 | 状态字段迁入 `__init__` + restore 实例隔离用例（§6 P3）|
| P3-2 穿越检查顺序 | ⏸ 保留（无实际风险） | `resolve()` 在前、原串 `..` 检查在后，逻辑等价于查入参，仅可读性 |
| P3-3 LLM 路径信任 | ⏸ 保留（设计权衡） | validate 只查格式不查归属，信任链依赖 LLM 提取质量 |
| P3-4 hash 计算开销 | ⏸ 保留（量级可忽略） | SHA-256 全量仅提交时一次 |
| P3-5 git 历史不可追溯 | ⏸ 保留（历史事实） | 无修复动作 |
| KG-2 相对路径放行 | ⚠️ known-gap | 测试 xfail 锁定，待修复（§6 KG-2 新增）|

### P1 — PlanStateSource 回注静默失效（memory/reinject/sources.py:362）

- **现象**：`META_KEY = "plan_wip_path"`，但**全仓库 grep 0 命中**该 key；run_core 实际写入 `session_meta["plan_file_path"]`（run_core.py:2515, 2639）。
- **根因**：sources.py 注释声称「由 plan 工具在 enter_plan_mode 时通过 set_session_meta 写入」——实际 plan 工具是 B2 纯信号、从不写 session_meta；写入方是 run_core，且 key 命名不同。API 漂移 + 注释与实现背离。
- **后果**：上下文压缩后 plan 内容**永不回注**，AI 可能遗忘计划细节偏离执行——**静默失效**（比崩溃难查）。
- **✅ 已修复（2025）**：`META_KEY = "plan_file_path"`（与 run_core 写入一致），docstring 同步修正为 run_core 写入方。回归锚：`memory/tests/test_plan_state_source.py` 字面量断言 + 10 用例覆盖（缺失/目录/空白/截断/非法 UTF-8/超长截断）。

### P2 — 模块零自动化测试覆盖

- pandaren/test、engine/tests 对 plan 关键词 0 命中：PlanManager 状态机（reminder 优先级 4 分支、filter_tools 3 规则、restore 两分支、_handle_plan_action 三向）全部无测试。
- 受影响：任何状态迁移改动都无回归网；§6 其余观察点的"修复后验证"也缺抓手。
- **✅ 已补齐（2025）**：55 用例 + 1 xfail 落地于 `plan/tests/test_plan_manager.py`（24）、`engine/tests/test_plan_action.py`（7，含 approve 写回 OSError 注入）、`memory/tests/test_plan_state_source.py`（10）、files 层（在 test_plan_manager.py 内）。已知差距 KG-2（`validate_plan_file_path` 放行相对路径，docstring 要求绝对路径）以 `pytest.xfail` 显式标记，待将来修复。

### P2 — approve 分支静默吞 OSError（run_core.py:3252）

```python
try:
    _write_plan(plan_file_path, edited_plan_content)
except OSError:
    pass
```
用户编辑内容写回失败时**无留痕**，与 O2/O3（故障隔离点必须 catch-log-转错误）冲突。后果：用户以为编辑已生效，实际磁盘仍是旧计划（静默偏离）。
- **✅ 已修复（2025）**：改为 `except OSError as e: logger.warning("[plan-action] failed to write back edited plan to %s: %s", ...)`。回归锚：`engine/tests/test_plan_action.py` A3 monkeypatch 注入 OSError 断言 catch-log 留痕。

### P3 — 其余观察点

| # | 位置 | 观察 |
|---|------|------|
| 1 | manager.py:37-43 | `_plan_file_path` 等声明为**类级默认值**再实例赋值——**✅ 已修复（2025）**：状态字段全部迁入 `__init__`（类级可变默认值会让所有实例共享状态，restore 一实例污染其它实例）|
| 2 | files.py:88-90 | 路径穿越检查基于**原始字符串**含 `..`；`resolve()` 已在前面执行（无害但顺序上先 resolve 再查原串，逻辑等价于查入参）|
| 3 | tools.py:67-75 | `plan_file_path` 参数**信任 LLM 提取**的用户路径——若 LLM 未在对话中找到路径而自行编造，validate 只查格式不查归属 |
| 4 | manager.py:268 | `compute_plan_hash` SHA-256 全量算，超大计划文件每次提交都有计算开销（量级可忽略）|
| 5 | git 历史 | plan 模块仅 Initial commit（c7d5e9f）一条，无独立演进记录可追溯设计迭代 |

### KG-2 — validate_plan_file_path 放行相对路径（known-gap，待修复）

- **现象**：`validate_plan_file_path`（files.py）docstring 要求「绝对路径」，但当前实现**放行含 `..` 的相对路径**（如 `./a/../b.md` 绕过 `..` 检查）、空文件名 `"x"` 等非绝对形式。
- **后果**：LLM 提供的相对路径不会因格式校验被拒——若 LLM 编造路径，可能落盘到非预期目录（写类工具场景下与 P3-3 叠加）。
- **✅ 已标记（2025）**：`test_validate_plan_file_path_rejects_relative_known_gap` 以 `pytest.mark.xfail(strict=True)` 显式锁定差距（断言真实执行，修复后 XPASS 即报警）。**修复方向**：校验开头改为 `path.is_absolute()`，拒绝一切非绝对路径；修复后移除 xfail 标记并补充正/反用例。

---

## 7. 测试与验证现状

| 维度 | 现状 | 影响 |
|------|------|------|
| 单元测试 | **已有 55 用例**（pytest 实测通过：plan/tests/test_plan_manager.py + engine/tests/test_plan_action.py + memory/tests/test_plan_state_source.py，覆盖 PlanManager 状态机 / reminder / filter_tools / files 校验 / 三向决策 / 回注）| 状态机/过滤/校验/回注回归网就位；1 xfail 标记 KG-2 相对路径 known-gap |
| 集成测试 | engine/tests 无 plan 用例；pandapal 侧 PlanModeManager 审批流不在本模块范围 | 端到端只靠手工验证 |
| 可测性 | files.py 纯函数天然可测；manager.py 无外部依赖可注入（仅依赖 files.read_plan）| 补测试成本低 |
| mock 友好度 | 工具 executor 只依赖 ToolContext（session_id/run_id）| 可脱离引擎单测 executor 校验逻辑 |

**建议的测试切入点**（若补）：① `get_reminder` 优先级 4 分支（含 methodology）；② `filter_tools` 三规则（只读保留/写类过滤/白名单）；③ `_handle_plan_action` 三向（refine 空消息 raise）；④ `validate_plan_file_path` 防穿越/后缀/父目录；⑤ restore 两分支（情况 A 继续规划 / 情况 B 决策）。

---

## 8. 与周边模块的契约

| 契约点 | 内容 | 违约后果 |
|--------|------|---------|
| run_core → PlanManager 公开接口 | enter/exit/reenter/is_planning/filter_tools/get_reminder/increment_turn/handle_tool_result/compute_plan_hash/read_plan_content | run_core 直改 `_plan_phase` 等私有字段将破坏封装（run_core.py:1391 有读私有字段的日志行，属观测性例外）|
| session_meta 三键 | `plan_phase` / `plan_file_path` / `plan_submitted_at`（+ `plan_summary`）| 键名漂移 = 跨 run 恢复失效（§6 P1 同源）|
| 工具名常量 | `enter_plan_mode` / `write_plan` / `exit_plan_mode` 字符串在 tools.py 定义，run_core 硬编码比较 | 改名需双处同步（当前无常量 import 打通）|
| StreamEvent | `PLAN_APPROVAL_REQUESTED`（engine/stream.py）带 plan_path + plan_content | 应用层（pandapal PlanModeManager）依赖此事件触发审批 UI |
| builder | `plan_mode(plan_dir=...)` → PlanToolFactory（builder.py:947-950，默认装配）| plan_dir 不传则默认 `{cwd}/.pandaren/plans/` |

---

## 9. 核心概念速查

| 概念 | 含义 |
|------|------|
| `PLAN_MODE_ALLOWED_BUILTIN` | 规划期白名单工具：enter / write / exit / ask_user（tools.py:43）|
| `TURNS_BETWEEN_REMINDERS = 5` | Sparse Reminder 注入间隔（manager.py:34）|
| `plan_complete` 分支 | exit 成功后的统一收尾：session_meta + 审计 + 事件 + run 终止 |
| `_handle_plan_action` | 用户三向决策处理（approve/refine/abandon）|
| `PlanStateSource` | 压缩回注 plan 文件正文的 source（已修复，见 §6 P1）|
| `write_initial_plan_file` | enter 时把用户原始需求写入计划文件开头 |
| 6-Phase 方法论 | Interview → Explore → Design → Review → Write → Submit（FULL_PLANNING_REMINDER 内容）|
