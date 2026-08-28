# 12. pandaren/hook — AgentHooks 统一生命周期协议

> 文件：`pandaren/hook/hooks.py`（539 行）| 测试：`pandaren/hook/tests/test_hooks.py`（708 行，U1~U17）| 设计：`pandaren/hook/tests/design/hooks.design.md`
> 定位：**横切扩展点**——引擎/工具/harness 的 21 个生命周期事件统一出口，观测、审计、应用层 hook 全挂在这里。

---

## 1. 模块概览

**一句话**：定义 21 个「可选实现」的生命周期扩展点（Protocol），引擎侧经 `_safe_hook` 字符串派发、harness/registry 侧直调，任何消费者异常都不传播进主流程。

**三个类**：

| 类 | 角色 |
|----|------|
| `AgentHooks` | `@runtime_checkable` Protocol，21 个方法签名（全部空体）——契约声明，不可实例化 |
| `DefaultAgentHooks` | 空默认实现（全 pass），给「只想覆写 1-2 个 hook」的消费者做基类 |
| `CompositeAgentHooks` | 组合器：按 add 顺序链式调用多个 hook 实例，单 hook 异常不中断后续，`_sig_cache` 签名内省缓存，`clone()` 浅拷贝 |

**21 个扩展点分 8 区**（hooks.py:1-32 模块文档）：

| 区 | 数量 | 事件 |
|----|------|------|
| A. Run 生命周期 | 2 | on_run_start / on_run_end |
| B. Step 生命周期 | 2 | on_step_start / on_step_end |
| C. LLM 调用 | 2 | on_before_llm_call / on_after_llm_call |
| D. Tool 执行 | 2 | on_before_tool_call / on_after_tool_call |
| E. Tool 管理 | 3 | on_tool_register / on_tool_discover / on_tool_disabled |
| F. Harness 事件 | 4 | on_tool_circuit_open / on_tool_circuit_close / on_tool_output_truncated / on_concurrent_execution_failure |
| G. 控制流事件 | 4 | on_hitl_requested / on_hitl_resolved / on_error / on_halt |
| H. Skill 生命周期 | 2 | on_skill_activated / on_skill_cleared |

---

## 2. 核心设计

### 2.1 三类转发模式（决定用例怎么分组）

| 模式 | 数量 | 方法 | Composite 转发行为 |
|------|------|------|--------------------|
| A. run 级固定转发 | 15 | on_run_start/end、on_step_*、on_before/after_tool_call、on_tool_discover/disabled、on_concurrent_execution_failure、on_hitl_requested/resolved、on_error、on_halt、on_skill_activated/cleared | 固定参数 + `session_id=session_id` |
| B. provider 条件转发 | 2 | on_before/after_llm_call | kwargs 恒含 model/tools/call_type/session_id；`provider` 仅当 `_accepts(h, method, "provider")` 为 True 时追加 |
| C. 非 run 级固定转发 | 4 | on_tool_register、on_tool_circuit_open/close、on_tool_output_truncated | 无 session_id、无 provider，仅转发声明参数 |

### 2.2 session_id 一等透传（数据隔离的核心机制）

- **run 级 hook（17 个）**：签名都声明关键字参数 `session_id: str = ""`，与 run_id 同为「本次 run 的归属凭证」，观测后端按会话分片。
- **单点注入**：引擎侧 `_safe_hook`（run_core.py:3115-3138）从 `self._current_session_id` 注入一次，避免 40+ 调用点逐一手传（DRY + 杜绝漏传）。
- **非 run 级（4 个）**：天然无会话归属，观测落 `_no_session`，属明确的「全局级」而非污染。

### 2.3 三个关键实现机制

**① `_safe_hook` 引擎侧统一派发**（run_core.py:3115）：

```python
def _safe_hook(self, method_name, *args, **kwargs):
    try:
        hooks = getattr(self, "_hooks", None)
        if hooks is None: return
        method = getattr(hooks, method_name, None)
        if method:
            kwargs.setdefault("session_id", getattr(self, "_current_session_id", "") or "")
            method(*args, **kwargs)
    except Exception as e:
        logger.warning("Hook %s raised exception (suppressed): %s", method_name, e)
```

**② Composite 容错 + 向后兼容**：
- 单 hook 抛 `Exception` → `_logger.debug` 记录后继续下一个（**不捕获 BaseException**，KeyboardInterrupt/SystemExit 会传播——U16 验证）。
- `_accepts` 签名内省：key=`(id(hook), method_name, param)` 缓存；VAR_KEYWORD 视为接受；inspect 失败（C 扩展）假定接受——**旧签名 hook（无 provider）不因 TypeError 失效**（U2/U3/U4 验证）。

**③ `clone()` 浅拷贝**（builder.py:909 旁注）：每 session 一份独立 Composite（`_hooks` 列表 + `_sig_cache` 独立），列表内元素共享引用——默认 hook 元素无跨会话 buffer，YAGNI 不深拷贝。

---

## 3. 消费者与触发时机（谁在什么时机调哪个 hook）

| 消费者 | 位置 | 调用的 hook | 时机 |
|--------|------|------------|------|
| `RunCoreMixin._safe_hook` | run_core.py:659-3103 | on_run_start/end、on_step_start/end、on_hitl_requested/resolved、on_error、on_halt、on_skill_activated/cleared、on_tool_discover | 引擎 8-Phase 循环各节点（run 开始/结束、每 step、LLM 调用前后、HITL 暂停/恢复、错误/终止、skill 激活/清除） |
| `HarnessExecutor` | behavior/harness/executor.py:240,290,327 | on_before_tool_call / on_after_tool_call | 工具执行前后（**自带 try/except，不复用 _safe_hook**，传 step_n + duration_ms） |
| `CircuitBreakerManager` | behavior/harness/circuit_breaker.py:131 | on_tool_circuit_open / close | 熔断器开/合 |
| `OutputGuard` | behavior/harness/output_guard.py:66 | on_tool_output_truncated | 工具输出超限截断 |
| `ToolFacade` | tool/facade.py:123,308,323 | on_tool_register / on_tool_disabled | 工具注册、禁用时 |
| `AgentBuilder._build_observability_layer` | builder.py:909-913 | —（组装） | `CompositeAgentHooks()` → add `obs_provider.hooks_adapter`（底座：logs/traces/metrics）→ add 用户 hooks（顶层） |

**Builder 装配链**（builder.py:909-913）：

```
CompositeAgentHooks
├── ObservabilityHooksAdapter   # 底座：19/21 hook（KG-1 缺 skill 两个）
└── 用户 hooks（如 SkillAwareHooks）
```

---

## 4. 与周边模块的契约

| 契约点 | 内容 | 违约后果 |
|--------|------|---------|
| `_safe_hook` 字符串派发 | run_core 用 `"on_run_start"` 等字符串调用；hook 方法缺失时 getattr 返回 None 静默跳过 | 拼错 hook 名 = 静默失效（无 warning） |
| session_id 签名 | 自定义 hook 必须接受关键字参数 session_id（可忽略），否则被注入打断 | 观测按会话分片留死角 |
| BaseException 边界 | Composite 只吞 `Exception`，不吞 KeyboardInterrupt/SystemExit | 人为约定，测试锁定（U16） |
| ObservabilityHooksAdapter 19/21 | adapter 缺 on_skill_activated/cleared（KG-1），靠 Composite 的 AttributeError 容错 | 依赖"吞异常"保证用户 hook 继续 |
| builder 双挂钩 | 观测底座先 add、用户 hook 后 add → 先观测后业务 | 顺序颠倒则用户 hook 异常会影响观测记录 |

---

## 5. 失败模式与风险

| # | 风险 | 状态 | 说明 |
|---|------|------|------|
| 1 | **on_skill_activated 协议僵尸参数 + 消费方签名漂移** | ✅ **2026-08-26 已修复** | 见下节详述 |
| 2 | hook 名拼写无编译期检查 | ⏸ 设计权衡 | `_safe_hook` 字符串派发，拼错静默失效；靠测试覆盖主路径 |
| 3 | 单 hook 性能风险 | ⏸ 量级可忽略 | 每次 LLM 调用 `_accepts` 有签名内省开销——已被 `_sig_cache` 缓存消除（U4 验证） |
| 4 | KG-1：adapter 缺 skill hook | ⏸ 已知差距 | 观测层不记录 skill 激活事件，靠应用层 SkillAwareHooks 兜底 |

### ✅ 风险 1 详述：`on_skill_activated` 协议僵尸参数与消费方签名漂移（2026-08-26 修复）

**现状（修复后）**：`python -m pytest pandaren/hook/tests/test_hooks.py` → **76 passed / 0 failed**（U1~U18，含新增 U18 回归）。

**原始问题（两个生产 bug，此前被误判为「测试侧 bug」）**：

时间线还原：
1. hooks.design.md v1 为 `on_skill_activated` 定义了 6 参协议（`skill_type`/`tools`）——**超前于领域模型**（Skill 无 skill_type 字段）
2. hooks.py 曾为 4 参、测试照抄 6 参 → 4 failed（本文件旧版 §5 记录的正是此状态）
3. 某次改动把 hooks.py 升级为 6 参 + 测试同步 6 参 → **75 passed**（测试变绿，但本文件未同步更新）
4. **生产 bug ①**：唯一消费方 `SkillAwareHooks.on_skill_activated` 保持 **4 参**签名未同步 → Composite 恒传 6 参 → TypeError → 被 `except Exception` 静默吞掉（仅 debug 日志）→ **SKILL_ACTIVATED 永不推送**
5. **生产 bug ②**：`app._register_skill_hooks` 只遍历一层 `_hooks` 找 SkillAwareHooks，而真实装配是**双层嵌套**（builder 外层 Composite → run_local 内层 Composite → SkillAwareHooks）→ 找不到 → `bind_broadcast` 从未调用 → 即使签名修好也推不出去
6. 两 bug 叠加：**前端永远收不到技能激活/清除事件**；且测试全绿（RecordingHook 用 `**kwargs` 接受一切，掩盖签名漂移）

**修复内容（2026-08-26）**：
1. **协议收敛 4 参**：hooks.py 三处（AgentHooks Protocol / DefaultAgentHooks / CompositeAgentHooks）`on_skill_activated` 删 `skill_type`/`tools`——该两参在协议、调用方、领域模型三层均无数据来源（僵尸参数），经确认删除，协议与调用方/消费方对齐
2. **递归查找**：app.py 查找逻辑提取为模块级 `_find_skill_hooks_in`，递归遍历嵌套 Composite 的 `_hooks` 列表（可独立测试）
3. **测试同步 + 回归**：test_hooks.py METHOD_PARAMS / _POS_ARGS / U17 同步 4 参；**新增 U18**（显式签名 hook 兼容回归）——防「`**kwargs` 掩盖签名漂移」复发
4. **设计文档同步**：hooks.design.md §1.3 参数表 + U17 详设 + 修订记录 v2

**经验教训**：
- 测试的 `**kwargs` hook（RecordingHook）会掩盖签名漂移——测试全绿 ≠ 生产正常；U18 用显式签名 hook 补上盲区
- 协议新增/修改参数必须**四处同步**：调用方（run_core）、消费方（SkillAwareHooks）、测试、设计文档
- 关联产出：pandapal/app.py `_find_skill_hooks_in` + 验证脚本 `scripts/_verify_hook_fix.py`

---

## 6. 测试与验证现状

| 维度 | 现状 | 影响 |
|------|------|------|
| 单元测试 | U1~U18 共 **76 用例**（参数化展开后；实测 76 passed / 0 failed，2026-08-26 全绿） | 协议一致性（U8：21 方法集合 + 签名约束）、容错、兼容、clone、顺序、透传全覆盖；U18 显式签名回归 |
| 覆盖矩阵 | P0×11 / P1×3 / P2×2 / P3×1 | 高优先级不变式全部有对应用例 |
| 集成测试 | U17 真实 ObservabilityHooksAdapter 链式 + InMemory 后端断言 | session_id 端到端落观测、provider 真实送达、KG-1 容错 |
| 可测性 | 零 mock（RecordingHook 家族有真实状态可审计）；仅 U4 用 monkeypatch 包装 inspect 做探测计数 | 高 |
| 引擎侧联动 | engine/tests/test_engine_mock.py 38-39 组 + observability/tests/test_data_isolation.py T19 覆盖 `_safe_hook` 的 session_id 注入 | 引擎 ↔ hook 契约有独立回归网 |

---

## 7. 关键结论

1. **hook 是 SDK 的观测总线**——21 个扩展点把引擎/工具/harness 生命周期事件统一出口，`CompositeAgentHooks` 让「内置观测 + 用户扩展」共存而非互斥。
2. **session_id 一等透传是核心隔离机制**——run 级 17 个 hook 全部带会话归属，`_safe_hook` 单点注入杜绝漏传；非 run 级 4 个明确全局。
3. **on_skill_activated 僵尸参数已删除、消费方签名漂移已修复**（2026-08-26）——协议 6 参 → 4 参（与领域模型/调用方/消费方对齐），app 查找改递归（`_find_skill_hooks_in`），U18 回归防复发。曾因「测试 `**kwargs` 掩盖签名漂移」+「只遍历一层绑定查找」双因叠加，导致前端收不到技能事件且测试全绿。
