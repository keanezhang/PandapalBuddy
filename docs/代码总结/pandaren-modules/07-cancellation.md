# 7. pandaren/cancellation — 协作式取消令牌（横切零依赖）

> 文件：`pandaren/cancellation.py`（117 行）| 设计契约：docs/design/取消语义-契约.md（本地）
> 定位：**横切模块**——贯穿 engine（L4）/ behavior（L3）/ tool（L2）/ sub_agent（编排层）四层的取消机制，与 constants.py / hook 同为「各层均可向下 import」的顶层模块。

---

## 1. 模块概览

两个类，117 行：

| 类 | 角色 |
|----|------|
| `CancelledSignal(Exception)` | 引擎内部协作式取消信号，**继承 Exception 而非 BaseException** |
| `CancelToken` | 协程安全的单向闸门：`cancel()` 永不复位；检查点 `raise_if_cancelled()` / 竞速 `wait()` / 级联 `link_parent()` |

---

## 2. 核心设计

### 2.1 为什么自定义 CancelledSignal 而非 asyncio.CancelledError（模块头注释）

- `CancelledError` 继承 **BaseException** → 绕过 O3 的 `except Exception` 兜底，有**逃逸出 run() 的风险**。
- 取消是「协作式、可转事件」语义（检查点抛 → per-step try 捕获 → 转 AGENT_CANCELLED 事件），不应复用 asyncio 的强制取消。

### 2.2 核心语义（模块头契约）

| 语义 | 实现 |
|------|------|
| 单向闸门 | `cancel()` 后永不复位（`_event.is_set()` 永真） |
| 干净起点 | 每次 run 在 `_run_stream_core` 入口重建 token，AgentLoop 可复用 |
| 下发通道 | 经 `ToolContext.metadata["cancel_token"]` 下发工具 / 子 Agent（P2/P3） |
| 幂等取消 | 多次 `cancel()` 只记录首个 reason |

### 2.3 三个使用姿势

```python
token.cancel("Cancelled by user")   # 外部触发（另一 task，如 STOP_GENERATION IPC）
token.raise_if_cancelled()          # 检查点：LLM 逐 chunk / 工具边界 / 子 Agent 处抛 CancelledSignal
await token.wait()                  # P2 工具竞速：asyncio.wait({tool_task, cancel_wait})
```

### 2.4 link_parent 父子级联（P3 子 Agent 级联，契约 §3.6 方案 B）

- 父取消 → 子同步取消：父的 Layer 0/1/2 检查点触发后，子的检查点也随之成立——**多层委派递归级联**。
- 父已取消走快速路径（同步 cancel，不依赖调度）。
- 返回后台监听 task。**调用方（委派处）必须在 finally 里 `task.cancel()` 解除链接**——否则子 Agent 实例被复用时残留父引用 → **跨 run 误取消**（契约 §10 风险项）。
- 必须在有运行 event loop 的上下文调用（委派发生在 run 期间，满足条件）。
- 内部有详尽日志（CASCADE 传播链路可观测）。

---

## 3. 与周边模块契约

| 消费方 | 用法 | 违约后果 |
|--------|------|---------|
| `RunCoreMixin._run_stream_core` | run 入口重建 token；per-step try 捕获 CancelledSignal → AGENT_CANCELLED + RUN_END | 取消后 AgentLoop 带脏状态复用 |
| HarnessExecutor（工具边界） | 工具调用前/后检查点 | 长耗时工具取消不响应 |
| 子 Agent 委派处 | `link_parent` + finally 解链 | 残留父引用 → 跨 run 误取消 |
| tool（P2 竞速） | `token.wait()` 竞速取消 | 无法中断正在执行的工具 |

---

## 4. 失败模式与风险

| # | 风险 | 状态 | 说明 |
|---|------|------|------|
| 1 | **link_parent 泄漏 → 跨 run 误取消** | ⚠️ 约定驱动 | 全靠委派处 finally 解链，无自动防护；契约 §10 已列为风险项。若某委派路径漏 finally，子 Agent 复用后会被上一次 run 的父取消误杀 |
| 2 | asyncio.Event 无 loop 创建 | ⏸ 已处理 | Python ≥3.12 允许无运行 loop 创建，loop 仅在 wait() 时惰性获取；AgentLoop.__init__（build 期）创建安全 |
| 3 | 取消检查点覆盖密度 | ⏸ 观察点 | 正确性依赖各层检查点「都调了 raise_if_cancelled」——LLM 逐 chunk / 工具边界 / 子 Agent 三处为契约明文，但任何新增长耗时路径漏检查点 = 取消延迟 |

---

## 5. 关键结论

1. **取消是协作式而非强制式**：`CancelledSignal` 继承 Exception 是关键决策——既被 O3 兜底捕获、又可转事件，不逃逸。
2. **单向闸门 + 每次 run 重建**：保证「取消永不复位」与「AgentLoop 干净复用」两个不变量同时成立。
3. **最大隐患在 link_parent 生命周期**：级联功能强大但依赖调用方 finally 解链，是纯约定保障的失效点。
