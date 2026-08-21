# 05 — pandaren/memory（记忆子系统）

> 模块总结 · 以代码为准（不依赖外部设计文档）· 锚点均为本次核实的 file:line
> 覆盖范围：`pandaren/memory/` 全部 16 个源文件（Facade + STM/LTM/WorkingMemory + compaction/ + reinject/ + backends/ + 协议与模型）
> 编号说明：现有序列 01-identity → 02-llm → 04-tool，本份顺延取 05（03 留白未占用；memory 在模块地图中位于 tool 之后）

## 1. 模块定位与职责

**一句话**：Memory 是 AgentLoop 与"记忆"交互的**唯一门面**（Facade），内部协调六个子系统，负责对话历史的增删查（ShortTermMemory）、持久化（LongTermMemory/RawLog）、运行态 KV（WorkingMemory）、异步刷盘（FlushPolicy），以及两种"防遗忘"机制——MicroCompact 清理旧工具结果、PostCompact 回注可枚举的 session 状态。

**最高原则**：SDK 自身零 LLM 调用（B3）——所有需要 LLM 的能力（摘要、token 精确计数）都定义为 Protocol，由应用层注入。SDK 内置实现全是确定性算法。

承载的三件核心事务：

1. **生命周期管理**：`init_from_restore` → append（user/assistant/tool）→ `compact_if_needed` → `flush_raw_messages` → `end_session`，五个 Phase 由 AgentLoop 按轮驱动
2. **上下文窗口控制**：token 估算 + 阈值触发 + 四层压缩管线 + 预算分配（system / 对话 / attachments 三块）
3. **会话状态桥接**：system 双层 prompt 管理、WorkingMemory KV（工具间传值）、暂停/恢复快照

覆盖文件（16 个源文件）：

```
pandaren/memory/
├── __init__.py           公共导出（Facade + 模型 + 协议 + 常量）
├── memory.py             ★ Memory Facade（1008 行，六组件编排者）
├── short_term.py         ShortTermMemory（STM：当前轮对话历史，纯内存）
├── long_term.py          LongTermMemory（LTM：raw_log 路由 + boundary 记录）
├── working_memory.py     WorkingMemory（session 级 KV，可选持久化）
├── flush_policy.py       AsyncBatchFlushPolicy（异步批量写，coalesce + 缓冲）
├── models.py             数据模型（MessageDict/CompactionSplit/MemorySnapshot 等 6 个）
├── protocols.py          协议（9 个 Protocol：估算/后端/策略/回注源）
├── constants.py          常量（压缩阈值/窗口参数/microcompact 参数）
├── estimators.py         TiktokenEstimator（真实 BPE 估算，本地词表离线加载）
├── compaction/
│   ├── windowed.py       WindowedKeepPolicy（默认切分策略）
│   ├── micro_compact.py  MicroCompactor（免费预清理 + 单条截断）
│   └── tool_pair_integrity.py  ensure_tool_pair_integrity（工具对完整性）
├── reinject/
│   ├── coordinator.py    PostCompactReinjector（压缩后回注编排）
│   └── sources.py        3 个内置 source（RecentFiles/ActiveSkills/PlanState）
└── backends/
    └── sqlite_raw_log.py SQLiteRawLogBackend（默认 raw_log 后端，单文件）
```

**依赖方向严格单向**：`facade → {protocols, models, constants, compaction, reinject} + 各实现`。对外依赖顶层 `constants.py`（CHARS_PER_TOKEN/DEFAULT_CONTEXT_WINDOW）与横切 `observability.Logger`；**不依赖** agent/engine/behavior/tool 层（仅 WorkingMemoryAccessor 被 tool 层反向消费，见 §11）。

---

## 2. 架构全景（六组件 + 数据流）

```
                        Memory Facade (memory.py:133)
        ┌──────────┬──────────┬───────────┬──────────┬──────────────┐
   ShortTermMem  LongTermMem  WorkingMem   FlushPolicy  MicroCompactor
   (memory.py    (long_term   (working_    (flush_      (compaction/
   →short_term   .py:37)      memory.py    policy.py    micro_compact
   .py:28)                    :38)         :19)         .py:63)
        └───────────────────────────────────────────────────────┘
                     PostCompactReinjector (reinject/coordinator.py:35)
                          ├─ RecentFilesSource (sources.py:75)  ─ 最近读的文件
                          ├─ ActiveSkillsSource (sources.py:225) ─ 激活技能
                          └─ PlanStateSource    (sources.py:346) ─ 当前 plan 文件
```

**数据流**（写入路径）：

```
AgentLoop 产出消息
   ├─ user      → append_user_message (memory.py:507)  同步直写 STM + raw_log（防崩溃丢）
   ├─ assistant → add_assistant_message (memory.py:518) STM 同步 + raw_log 异步入队
   └─ tool      → add_tool_result (memory.py:530)       入口即 MicroCompact 单条截断 + 异步入队
                                                              │
                          AsyncBatchFlushPolicy (flush_policy.py:19)
                          coalesce 100ms + 缓冲 50 条 → SQLiteRawLogBackend
```

**system 消息由 Facade 统一管理**（memory.py:363 `_build_system_content`）：
`CORE_PROMPT + agent_config 双层 prompt` + `<attachment-memory>` 回注区。`set_system_prompt`（memory.py:917）是运行时可变态（HC1 的具名例外，见 §9）。

---

## 3. 消息生命周期（5 Phase）

| Phase | 方法 | 要点 |
|-------|------|------|
| 1 初始化 | `init_from_restore` (memory.py:418) | **三档恢复优先级**：① STM 快照命中同 session（零 IO）→ ② raw_log 按 token 预算恢复（backends/sqlite_raw_log.py:256 `load_within_budget`）→ ③ 全新对话。恢复后叠加 `ensure_tool_pair_integrity` 兜底 |
| 2 追加 | `append_user_message` / `add_assistant_message` / `add_tool_result` | 用户消息**同步直写**（防崩溃丢失），assistant/tool 走**异步批量**（memory.py:575 `_enqueue_message_async` vs 597 `_enqueue_last_message` 双路径）；`add_tool_result` 入口即做 MicroCompact 单条截断（memory.py:530） |
| 3 压缩 | `compact_if_needed` (memory.py:644) | 四层管线（见 §4） |
| 4 刷盘 | `flush_raw_messages` (memory.py:815) | session 结束前强制落盘（flush 全部 session key） |
| 5 结束 | `end_session` (memory.py:837) | **v1.4 去 summary 化**：只 flush + reset，不再调 LLM 生成摘要——跨 session 召回废弃，知识提炼移交应用层定时任务消费 raw_log |

暂停/恢复辅助：`snapshot_for_pause` (memory.py:946) + `resume_context` (memory.py:960)——支持 HITL 暂停后恢复，快照含 STM 消息、WorkingMemory、session_meta、run_id。

---

## 4. 四层压缩管线（核心机制）

`compact_if_needed`（memory.py:644，触发阈值 `compact_threshold` = context_window × conversation_ratio，constants.py:20）：

```
estimate_tokens > threshold?
  [L1] MicroCompactor.clear_old_tool_results (micro_compact.py:119)
       → 清早期白名单工具结果正文（保留最近 3 条不动，constants.py:61）
       → 省下的 token 够则提前 return（不写 boundary、不触发 cache 冷启动）
  [L2] CompactionPolicy.split()（默认 WindowedKeepPolicy.split，windowed.py:113）
       → 工具对完整性兜底（tool_pair_integrity.py:57）
       → 反扩保护：kept >= original 时丢弃压缩（防御自定义策略实现缺陷）
  [L3] DropSummarizer.summarize(dropped)（应用层可选，异步 LLM；protocols.py:255）
       → 产物插 kept 前（role=system）
  [L4] 写 CompactBoundary（long_term.py:69）+ on_compact_callback（LLM cache 冷启动，
       memory.py:627 set_on_compact_callback）+ PostCompact 回注
  返回: None=OK / int=仍超阈值 → Context Overflow（调用方 AgentLoop 应终止）
```

**WindowedKeepPolicy 窗口约束**（windowed.py:88，三个下限一个上限）：
- `DEFAULT_MIN_KEEP_TOKENS` 8K（上下文深度，constants.py:39）
- `DEFAULT_MIN_KEEP_TEXT_MESSAGES` 4（对话连续性，防窗口全是 tool result，constants.py:42）
- `DEFAULT_MAX_KEEP_TOKENS` 40K（硬上限，防压缩后立即再触发，constants.py:45）

**CompactBoundary 是 raw_log 的"断层标记"**（models.py:32）：`load_within_budget` 以此为恢复起点（只认最新 boundary），早于 boundary 的历史不再回读。

---

## 5. 两个"防遗忘"机制（语义边界划分清晰）

| | DropSummarizer | PostCompactSource |
|---|---|---|
| 对象 | 被**丢弃**的消息 | session 内**可枚举**的当前状态 |
| 手段 | 应用层注入、异步、可调 LLM（protocols.py:255） | 只读 WorkingMemory/SkillRegistry/session_meta，**不调 LLM** |
| 产物 | 一条 role=system 摘要，插 kept 前 | 多条 role=user attachment，插 system 后对话前 |
| 定位 | 业务可选 | SDK 内置 3 个 source（reinject/sources.py） |

设计巧思：PostCompact 回注**不依赖语义匹配**——用户说"继续"这种短指令时，压缩后仍能精确补回"刚读的文件 / 激活的技能 / 进行中的 plan"。附件用 `role=user`（可多条、LLM 注意力更高）而非 system，外壳用 `<post-compact-context>` XML 标记便于解析。总预算由 `DEFAULT_POST_COMPACT_TOKEN_BUDGET` 控制（默认 50K），超预算从后往前丢弃。

三个内置 source 的采集对象：
- **RecentFilesSource**（sources.py:117）：读 WorkingMemory 的 `recent_file_reads` 键 → 读文件内容 → token 截断
- **ActiveSkillsSource**（sources.py:258）：从 `ctx.skill_registry` 取激活技能 → 头部信息
- **PlanStateSource**（sources.py:369）：从 `ctx.session_meta` 取 plan 文件路径 → 读文件 → token 截断

---

## 6. 工具对完整性（API 硬约束）的三道防线

从真实事故（`sess-f537efb5`：token 截断切在 assistant/tool_result 之间 → OpenAI 兼容 API 400）沉淀出的防御。`ensure_tool_pair_integrity`（tool_pair_integrity.py:57）规则：**assistant 带 tool_calls 则其后的 tool_result 必须保留；孤儿 tool_result 若与已删的 assistant 成对也一并删除**。

三道防线：

1. **策略内**：`WindowedKeepPolicy.split()` 内部执行（windowed.py:113 体内）
2. **压缩后叠加**：`compact_if_needed` L2 对自定义策略再兜底一次（memory.py:644 体内）
3. **出站守卫**：`get_messages()`（memory.py:979）每次返回前都校验——把"压缩时才校验"扩大为"每次出站都校验"，拦截任何路径产生的孤儿 tool_call；守卫不修改入参，返回新列表

---

## 7. 数据模型与协议层

**models.py 六个模型**：

| 模型 | 行号 | 说明 |
|------|------|------|
| `MessageDict` | 22 | 消息 TypedDict（role/content/tool_calls/tool_call_id） |
| `CompactBoundaryDict` | 32 | boundary 记录（seq/session_id/created_at） |
| `CompactionSplit` | 47 | 切分结果（kept/dropped，不可变） |
| `MemorySnapshot` | 66 | 暂停快照（STM + WM + meta + run_id） |
| `ReinjectionAttachment` | 80 | 回注附件（role/name/content/token） |
| `PostCompactContext` | 98 | 回注上下文（session_id/skill_registry/session_meta） |

**protocols.py 九个协议**（SDK 只定义协议，实现全插件）：

| Protocol | 行号 | 注入点 |
|----------|------|--------|
| `TokenEstimator` | 40 | `.memory(token_estimator=...)` |
| `CharBasedTokenEstimator` | 54 | SDK 默认实现（chars/4，零依赖） |
| `WorkingMemoryAccessor` | 85 | 暴露给 tool 层的只读访问面 |
| `WorkingMemoryBackend` | 105 | WM 持久化后端 |
| `RawLogBackend` | 142 | 对话原始日志后端 |
| `CompactionPolicy` | 218 | 切分策略（split） |
| `DropSummarizer` | 255 | 丢弃消息摘要 |
| `FlushPolicy` | 286 | 异步刷盘策略 |
| `PostCompactSource` | 320 | 压缩后回注数据源 |

**token 估算双实现**：
- `CharBasedTokenEstimator`（protocols.py:54）：`chars / CHARS_PER_TOKEN`，对中文/代码系统性低估 ~2x（压缩触发过晚）
- `TiktokenEstimator`（estimators.py:53）：真实 BPE（cl100k_base），支持本地 vendored 词表 + hash 校验绕开网络下载（estimators.py:37-50）；cl100k_base 对中文优化 BPE 是 ±10-20% 近似，但已是数量级修正；**fail-fast 不静默降级**（未装 tiktoken 抛 ImportError，由应用层决定是否降级）

---

## 8. 设计原则落地情况（不变量 → 代码证据）

| 原则 | 落地 |
|------|------|
| HC1 | `_FROZEN_ATTRS`（memory.py:324）+ `__setattr__` 拦截（memory.py:342）运行时改配置；`set_system_prompt`（memory.py:917）用 `object.__setattr__` 构成**具名受控的例外**——双层 Prompt 设计下 system_prompt 本就是运行时可变态，注释将这条 HC1 例外讲明 |
| HC2 | `get_messages()` / `post_compact_attachments`（memory.py:938）均深拷贝返回 |
| HC8 | 全层 TypedDict（MessageDict/CompactBoundaryDict/ReinjectionAttachment）+ frozen dataclass（CompactionSplit） |
| B3 | SDK 内置实现零 LLM：估算（字符/BPE）、切分、回注、刷盘全是确定性算法 |
| E4/O3 | 后端失败 log warning 不崩溃，但不静默吞异常（捕获均带 logger） |
| 尺子统一 | 同一个 `token_estimator` 实例注入 STM/Policy/Backend，估算口径一致 |
| 只读面收窄 | WorkingMemory 暴露给工具的是 `accessor`（working_memory.py:196）——只有 get/set，**没有 clear**（clear 是 Loop 的职责，防工具清空会话状态） |

---

## 9. 工程亮点

1. **run_id/step 随 raw_log 落独立列**（backends/sqlite_raw_log.py:196 append 时透传）——与 traces 按 (run_id, step) **key join** 而非顺序对齐，多 run/多会话不错位
2. **`_with_timestamp` 只补展示不污染**（memory.py:564）：`{**msg}` 生成新 dict，timestamp 进 raw_log 但**不进 LLM 请求**
3. **SQLite 构造参数互斥校验**（backends/sqlite_raw_log.py:106）：`db_path` 与 `connection` 严格互斥、拒绝 `:memory:`——把"看似合理"的用法在构造期挡掉
4. **旧库迁移**（backends/sqlite_raw_log.py:165 `_migrate_reasoning_content`）：用 `PRAGMA table_info` 判断列存在性而非盲 try/except
5. **L1 免费预清理可短路整条管线**：MicroCompact 清完若已够，不写 boundary、不触发 cache 冷启动、省一次切分
6. **反扩保护**：kept >= original 丢弃压缩，防御自定义切分策略的实现缺陷

---

## 10. 风险点与改进建议（供后续深挖）

| # | 风险 | 位置 | 说明与建议 |
|---|------|------|-----------|
| 1 | **首条消息可突破 budget** | backends/sqlite_raw_log.py:256 | `load_within_budget` 中 `accumulated + t > budget and keep_count > 0` 才 break——keep_count==0 时只累加不中断。"至少保留一条"是有意为之但**未注释**；单条巨型消息会突破 budget。建议补注释或在文档中明示语义 |
| 2 | **seq 生成并发窗口** | backends/sqlite_raw_log.py:178 `_next_seq` | 用 `MAX(seq)+1` 而非独立计数器。单进程 SQLite 写事务串行化下安全（DEFERRED 事务首次写升级写锁），但**多进程**同写一个 db 文件时存在同 seq 竞争。WAL 模式下是实际风险，应用层需保证单写者 |
| 3 | **PostCompact 预算与阈值张力** | memory.py:644 L4 | 回注预算默认 50K 且 attachments 在 L4 才收集，`target_tokens` 只扣旧 overhead——压缩后新回注可能再次推超阈值返回 overflow（设计内行为，最后一层防线）。默认参数下 50K 回注 + 40K keep + system 很可能触发；应用层应调小 budget 或提高阈值 |
| 4 | **RecentFilesSource 无 size 上限防护** | reinject/sources.py:117 | 路径来自 WorkingMemory（应用层工具写入），直接 `open(path)` 只做 token 截断不做字节上限。本地 Agent 场景下与工具权限等价不算漏洞，但超大文件会白读 IO |
| 5 | **切分近似** | protocols.py:54 CharBased | chars/4 对中文/代码低估 ~2x；已提供 TiktokenEstimator 修正，但默认仍是 CharBased——应用层需显式注入才能受益 |

---

## 11. 对外接口（谁消费 Memory）

- **AgentLoop**（`engine/run_core.py`）：按 Phase 调 5 个方法 + `set_run_context`（memory.py:556）/ `get_messages`（memory.py:979）；`compact_if_needed` 返回 int 即触发 Context Overflow 终止
- **MessageBuilder**：消费 `get_messages()` 拼 LLM payload
- **LLMClient**：经 `set_on_compact_callback`（memory.py:627）被通知压缩后 cache 冷启动
- **Tool**：通过 `working_memory_accessor`（memory.py:903，只读面）做 get/set，无 clear
- **PostCompact 回注**：`ActiveSkillsSource` 从 `ctx.skill_registry` 反向读 skill 层；`PlanStateSource` 读 session_meta 的 plan 路径
- **应用层注入**（对应 PANDAPAL.md"11 个可替换扩展点"中记忆相关 4 个）：`raw_log_backend` / `compaction_policy` / `drop_summarizer` / `token_estimator`，另加 `flush_policy` / `working_memory_backend` / `post_compact_sources` 共 7 个注入位，全部经 `AgentBuilder.memory(...)`

---

## 12. 与上下篇印证关系

- **下篇（预期）**：`behavior` 层的 ContextWindowBudget 与 memory 阈值口径一致（`DEFAULT_COMPACT_THRESHOLD = context_window × conversation_ratio`，constants.py:20 注释明示"与 ContextWindowBudget 一致"）；`engine/run_core.py` 的 8-Phase 循环与本文 §3 的 5 Phase 消息生命周期对接。
- **依赖边界**：memory 层是纯能力层（capability Layer 2），不反向依赖上层；WorkingMemoryAccessor 协议被 tool 层消费（tool.py 定义 Tool 时依赖 `memory.protocols`），与 04-tool.md §1 所述一致。

---

## 13. 复验记录

- ✅ file:line 锚点抽查（本次精读全命中）：memory.py（Memory:133 / init_from_restore:418 / compact_if_needed:644 / end_session:837 / get_messages:979 / set_run_id:1001）、short_term.py（ShortTermMemory:28 / split_with:115）、long_term.py（append_compact_boundary:69 / load_for_restore:85）、working_memory.py（WorkingMemory:38 / accessor:196）、flush_policy.py（AsyncBatchFlushPolicy:19 / enqueue:42）、windowed.py（split:113）、micro_compact.py（clear_old_tool_results:119）、tool_pair_integrity.py:57、coordinator.py:35、sources.py（三个 source 类：75/225/346）、protocols.py（9 个 Protocol：40/54/85/105/142/218/255/286/320）、estimators.py:53、sqlite_raw_log.py（_next_seq:178 / load_within_budget:256）、models.py（6 个模型：22/32/47/66/80/98）、constants.py（阈值:20 / 窗口参数:39-45 / microcompact:58-64）
- ✅ 事故案例（sess-f537efb5）与 v1.4 去 summary 化决策均已在代码注释中核实（memory.py:837 end_session 注释、tool_pair_integrity.py 头部注释）
