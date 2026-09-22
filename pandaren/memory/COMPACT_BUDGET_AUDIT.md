# 上下文预算实测体检报告（Compact Budget Audit）

- 状态：**待治理**
- 测量日期：2026-09-22
- 测量工具：`scripts/measure_context_budget.py`（可重复执行）
- 计数口径：**真实 BPE tokenizer `cl100k_base`**（与 `run_local._build_token_estimator()` 注入的一致）
- 测量对象：**真实实例化**（真 `PromptAssembler`、真 `get_all_tools()` + 4 个 Provider 类、真 `ToolBudget`），不引用任何代码常量或注释

配套文档：

| 文档 | 作用 |
|---|---|
| `COMPACT_THRESHOLD_SPEC.md` | 阈值分配规范（设计目标） |
| `COMPACT_THRESHOLD_MAP.html` | 可视化（含"实测修正 / 原始 spec"口径切换） |
| **本文档** | **实测事实 + 治理清单** |

---

## 0. 一句话结论

**规范里那套"从 T 按比例反推一切"的方法，对"固定尺寸的槽位"是错的。实测显示：`system_prompt` 在 coding 模式已超配额 1.15x，`RESERVED` 被高估约 10 倍，`tool_schema` 被高估约 7 倍，`recall` 的 10,000 配额给了一个已废弃的功能。**

---

## 1. 实测结果

### 1.1 system_prompt（`PromptAssembler` 真实拼装，工作区 = 本仓库）

| 成分 | 字符 | **真实 token** | 占 system_prompt |
|---|---|---|---|
| `PROMPTS['coding']`（CODING_PROMPT + TEST_RULE） | 3,657 | 2,977 | 17% |
| `env_block` | 266 | 169 | 1% |
| **`PANDAPAL.md`** | **33,259** | **14,045** | **82%** |
| `CODING_RULES.md` | 0 | 0 | 缺失（本仓库无此文件） |
| **`system_prompt[coding]` 合计** | **37,207** | **17,203** | 100% |
| `PROMPTS['office']` | 1,917 | 1,680 | 91% |
| `env_block`（office 共用） | 266 | 169 | 9% |
| **`system_prompt[office]` 合计** | **2,184** | **1,849** | 100% |

> **coding / office = 9.3 倍。**
> `PANDAPAL_RULE.md` **不会被注入**（无任何代码读取它）。
> 片段 `CODING_RULES.md` / `soul.md` / `OFFICE_RULES.md` 在本仓库不存在；若用户工作区提供，会计入对应 mode。

### 1.2 static_context（拼进 system message 末尾的三段 XML）

| 成分 | 字符 | **真实 token** | 备注 |
|---|---|---|---|
| `build_skill_summaries()`（4 个 skill，经 1% 预算裁剪后未裁） | 851 | **346** | `MessageBuilder.build_static_context_str` 真实渲染 |
| `build_agent_summaries()`（2 个蓝图） | 317 | **41** | ⚠️ 蓝图 `name`/`description` 字段取值为空，**偏低估** |
| **小计** | **1,168** | **387** | 不含工具目录 |

### 1.3 tool_schema（13 个真实 Tool → 真实 OpenAI `tools=[...]` payload）

工具来源：`get_all_tools()` 2 个（web）+ Provider 类 11 个（`AgentTaskTools` 7 / `SchedulerTools` 2 / `ProgressTools` 1 / `AppDataTools` 1）。

| 工具 | description 字符 | **真实 token** |
|---|---|---|
| `create_agent_task` | 587 | **585** |
| `report_progress` | 485 | 497 |
| `push_app_data` | 345 | 441 |
| `update_agent_task` | 317 | 343 |
| `create_scheduled_task` | 196 | 320 |
| `verify_agent_task` | 440 | 307 |
| `web_search` | 94 | 220 |
| `set_task_dependency` | 70 | 155 |
| `web_fetch` | 31 | 133 |
| `list_agent_tasks` | 95 | 119 |
| `get_agent_task` | 43 | 88 |
| `delete_agent_task` | 37 | 88 |
| `delete_scheduled_task` | 12 | 78 |
| **合计** | 2,752 | **3,374** |
| 真实 payload（含 JSON 结构） | 7,474 | 3,388 |
| 平均 / 工具 | | **260** |

### 1.4 其余固定项

| 项 | **实测** | 来源 |
|---|---|---|
| `RESERVED`（摘要输出上限） | **512 token** | `LLMDropSummarizer` 实际下发 `ModelSettings(max_tokens=512)` |
| 摘要 prompt 自身 | 63 token | `DEFAULT_SUMMARY_PROMPT`，57 字符 |
| `REINJECT` | **0** | pandapal 未注入 `post_compact_sources` |
| `TOOL_RESULT_CAP` | **20,000** | `DEFAULT_MICROCOMPACT_SINGLE_RESULT_MAX_TOKENS` |

### 1.5 固定开销合计 vs 配置配额

| | coding | office |
|---|---|---|
| `system_prompt` | **17,203** | **1,849** |
| `static_context` | 387 | 387 |
| `tool_schema` | 3,374 | 3,374 |
| **固定开销合计** | **20,964** | **5,610** |

| 对照项 | 值 |
|---|---|
| config `system_prompt` 配额 = 100,000 × 0.15 | 15,000 |
| config `tool_schema` 配额 = 100,000 × 0.10 | 10,000 |
| config `recall` 配额（未传 → 默认 0.10） | 10,000 |
| config `conversation` 配额 = 100,000 × 0.65 | 65,000 |
| → 实际 `compact_threshold` | **65,000** |
| `system_prompt[coding]` / system 配额 | **1.15x ❌ 超配额** |
| `tool_schema` / tool 配额 | **0.34x ✅** |
| 固定开销 / (system+tool 配额) | 0.84x ✅ |
| **被闲置的配额**（recall 10,000 + tool 余量 6,626 − system 超支 2,203） | **14,423** |

### 1.6 截断效应实测（`TOOL_RESULT_CAP`）

单条上限 20,000，实测 `truncate_single_result_if_needed`：

| 内容类型 | 原始 token | **截断后 token** | 达标 |
|---|---|---|---|
| 英文（4 字符/token） | 80,001 | 17,749 | ✅ |
| 代码（~2.5 字符/token） | 110,000 | **32,527** | ❌ 1.6x |
| **中文（1 字 ≈ 1 token）** | 64,000 | **64,015** | ❌ **3.2x，且比原文更大** |

### 1.7 `ToolBudget.enforce` 裁剪实测

`max_always_count = 15`（硬下限，`budget.py:19`）。

| 场景 | 输入 | 其自估 token | 预算 | 裁剪结果 |
|---|---|---|---|---|
| 仅应用 13 个 | 13 | 2,614 | 10,000 | **0 个被裁** |
| 13 应用 + 10 个 MCP（模拟） | 23 | 3,674 | 10,000 | **0 个被裁** |

> 同一批 13 个 schema：真 tokenizer **3,374** vs `ToolBudget` 自估 **2,614** → **相差 22.5%**。

---

## 2. 实测 vs 规范：逐项偏差

| 字段 | 规范（`COMPACT_THRESHOLD_SPEC.md`） | **实测** | 偏差 |
|---|---|---|---|
| `SYS_ACTUAL` | 6,000（平衡）/ 8,000（激进） | **17,203（coding）/ 1,849（office）** | coding **低估 2.1~2.9x**；office **高估 3.2~4.3x** |
| `system_prompt` slot | `min(0.15 CW, 16,000)` | 需要 **≥ 17,203** | ❌ **已被突破 1.15x** |
| `TOOL_CAP` / `tool_schema` slot | `min(0.10 CW, 24,000)` | **3,374** | ❌ **高估 7.1x** |
| `RESERVED` | `0.08 T` = 4,800 / 6,360 | **512** | ❌ **高估 9.4~12.4x** |
| `REINJECT` | `min(0.15 T, 60,000)` | **0**（未启用） | ❌ 无依据 |
| `TOOL_RESULT_CAP` | `min(0.15 T, 30,000)` | 代码默认 **20,000** | ⚠️ 与实现不一致 |
| `recall_ratio` | 0.00（规范已建议置 0） | config 仍传默认 **0.10** | ❌ 10,000 闲置 |
| `MIN_KEEP` / `KEEP_CAP` | `0.12 T` / `0.45 T` | **无法测量**（需压测） | ⚠️ 未验证 |

### 同一批数据的三套口径

| 位置 | 口径 | 对同一批 13 个 schema 的结果 | 对 `system_prompt[coding]` 的结果 |
|---|---|---|---|
| `TiktokenEstimator`（app 注入） | 真实 BPE | **3,374** | **17,203** |
| `ToolBudget._estimate_tokens` | `utf-8 bytes / 4` | 2,614（低 22.5%） | — |
| `engine/loop.py:214` 截断判据 | `chars / 4` | — | **9,302（低 1.85x）** |
| `CharBasedTokenEstimator`（SDK 默认） | `chars / 4` | — | 9,302（低 1.85x） |
| `micro_compact` 裁剪 | `tokens × 4` 字符 | — | 中文场景**反而变大** |

---

## 3. 五个关键结论

### 结论 1 · `system_prompt` 配额在 coding 模式已被突破，且程序发现不了

- 实测 17,203 vs 配额 15,000 = **1.15x**
- 截断判据（`engine/loop.py:213-235`）用 `len(system_prompt) / CHARS_PER_TOKEN`：
  - coding：37,207 / 4 = **9,302**（程序以为）vs **17,203**（实际）→ **低估 1.85x**
  - office：2,184 / 4 = **546** vs **1,849** → **低估 3.39x**
- 所以它算出"还剩 5,698"，实际已经**超支 2,203**，**永远不会报警**
- **根因**：用 ratio 给固定尺寸槽位配额。coding 要 17,203、office 只要 1,849，差 **9.3 倍**，一个固定比例不可能同时合适

### 结论 2 · `PANDAPAL.md` 是决定项，且随用户工作区变化

- **14,045 / 17,203 = 82%** 的 system prompt 来自用户工作区的一个文件
- `PromptAssembler` 按 sha256 热重载（`prompt_fragments.py:91-132`）→ 换工作区，system_prompt 可从 3K 变到 20K+
- **`SYS_ACTUAL` 在原理上不该是常量**

### 结论 3 · `tool_schema` 当前够用，但 MCP 是不受控变量

- 实测 3,374 / 配额 10,000 = **34%**，规范给的 24,000 是**高估 7.1x**
- 风险在定性侧：
  - `_safe_schema`（`mcp/tool_adapter.py:178-183`）**原样透传** MCP 的 `inputSchema`，体积由第三方决定
  - `tier = self._cfg.tier`，MCP 可配成 **ALWAYS**
  - `ToolBudget.enforce` 是 `while total > budget and len(schemas) > 15: schemas.pop()` → **总长度 ≤ 15 时预算整体失效**；13 个 ALWAYS + `search_tools` = **14**，已在失效边缘

### 结论 4 · `RESERVED` 被高估约 10 倍

- 实测摘要输出上限 = **512 token**（`ModelSettings(max_tokens=512)`）
- 规范给 `0.08 T` = 4,800 / 6,360

### 结论 5 · `TOOL_RESULT_CAP` 截断在中文场景**失效且反向**（真 bug）

- 中文 64,000 token 的内容：判定"超限"→ 走到裁剪 → `cut_at = 20,000 × 4 − 200 = 79,800` 字符 → 而 64,000 汉字只有 64,000 字符 **< 79,800** → `text[:79800]` = 全文 → 加后缀 → **64,015 token（比原文更大）**
- 根因：**判定用真实 tokenizer，裁剪用 `CHARS_PER_TOKEN = 4.0`**

---

## 4. 治理清单（逐项处理）

> 优先级：P0 = 会直接导致错误行为；P1 = 造成资源浪费/配额误判；P2 = 一致性/可维护性

### 状态总览（2026-09-22 更新）

| 条目 | 状态 | 批次 | 落地内容 |
|---|---|---|---|
| G1 system_prompt 改绝对值 | ✅ 已修复 | B1 | `ContextWindowBudget` 新增绝对槽位；app 用 24,000 |
| G2 截断判据改用 estimator | ✅ 已修复 | B2 | `Memory.estimate_text` / `truncate_text_to_tokens`；`loop.py` 改用 |
| G3 片段独立预算 | ⬜ 待做 | B4 | |
| G4 recall 置 0 | ✅ 已修复 | B1 | `DEFAULT_RECALL_RATIO = 0.00`；app 显式传 0 |
| G5 tool_schema 改绝对值 | ✅ 已修复 | B1 | app 用 8,000 |
| G6 MCP `inputSchema` 封顶 | ⬜ 待做 | B4 | |
| G7 `ToolBudget` 下限语义 | ⬜ 待做 | B4 | |
| G8 `RESERVED` 改实测 512 | ✅ 已修复 | B1 | `DEFAULT_RESERVED_SUMMARY_TOKENS = 512` |
| G9 `REINJECT` 未启用不占预算 | ✅ 已修复 | B1 | 默认 50,000 → 8,000；未注入 sources 时预留为 0 |
| G10 截断口径（中文越截越大） | ✅ 已修复 | B2 | 二分收敛：中文 64,000 → 19,999 |
| G11 统一 5 套口径 | 🟡 部分 | B2/B3 | `loop.py` + `micro_compact` 已统一；`ToolBudget` 待改（B4） |
| G12 测量台接 CI | 🟡 部分 | B3 | `--check` 断言模式已加，当前退出码 0；CI 接线待做 |
| G13 `MIN_KEEP` 压测校准 | ⬜ 待做 | B4 | 需压测框架 |
| G14 摘要预算补传 `context_window` | ✅ 已修复 | B2 | `loop.py:199,203` 补传 |
| G15 `POST_COMPACT_TOKEN_BUDGET` 下调 | ✅ 已修复 | B1 | 50,000 → 8,000 + 不变式告警 |
| G16 模型窗口映射表的来源与生命周期 | ⚠️ 已加防护，**需持续维护** | B1 | 见下节 |

### G16 · 模型窗口映射表：数据来源与生命周期

| 项 | 内容 |
|---|---|
| 文件 | `pandapal/config/llm/model_context_windows.toml` |
| **结构性风险** | 该表是**静态数据**，而厂商的"稳定别名"背后服务端版本会漂移 |
| 实例 | `deepseek-chat`：V3.x = **128K** / V4 = **1M**。最初写死 `deepseek* = 131072`（V3 的值）→ V4 用户的 1M 窗口**只按 128K 配预算**，正好复现"阈值不跟模型走"的老问题 |
| 已核实条目 | DeepSeek V3.1/V3.2 = 128K（官方 2025-08 公告）；DeepSeek V4 Preview = **1M**（deepseek.com 官方新闻）；Claude 系 = 200K |
| **已删除条目** | `gpt-4o*` / `gpt-4.1*` / `gemini-*` / `qwen*` / `glm-4*` / `moonshot*` —— 我此前凭印象写的值**无任何来源**，写错比不写更糟（写小浪费窗口，写大直接 400） |
| 兜底行为 | 未声明 → `[default] 128,000` + **WARNING**（不静默降级） |
| 应急口 | 环境变量 `PANDAPAL_MODEL_MAX_CONTEXT`（**最高优先级**，改完即生效、不用改代码） |
| 维护规范 | 每条附 `# 来源/日期`；BYOK 场景**优先用 `[models]` 显式声明**自己在用的模型 |

### G1 · `system_prompt` 配额改为绝对值，并按 mode 动态
- **现象**：coding 实测 17,203 > 配额 15,000（1.15x）
- **证据**：§1.1、§1.5
- **位置**：`pandapal/local/run_local.py:520-525`
- **建议**：`system_prompt` 从 ratio 改为绝对值（建议 24,000，留 40% 余量给更大的工作区）；或改由「每次 run 实测后动态写入」
- **优先级**：P0 ｜ **工作量**：小

### G2 · `static_context` 截断判据改用注入的 estimator
- **现象**：判据用 `len(text)/4`，对中文混排低估 1.85x（coding）/ 3.39x（office），配额超支无法被发现
- **位置**：`pandaren/engine/loop.py:213-235`
- **建议**：改用 `ContextWindowBudget` 同步下发的 token 口径，或传入 Memory 使用的 `token_estimator`
- **优先级**：P0 ｜ **工作量**：小

### G3 · 给工作区 prompt 片段单独设预算
- **现象**：`PANDAPAL.md` 14,045 token（占 system_prompt 82%），无任何独立上限
- **位置**：`pandapal/local/prompt_fragments.py:50-55`、`161-168`
- **建议**：为 `FragmentSpec` 增加 `max_tokens`，超限则截断该片段（而非让整体 system 失控）
- **优先级**：P1 ｜ **工作量**：中

### G4 · `recall_ratio` 置 0，回收 10,000 配额
- **现象**：config 未传 → 默认 0.10，而 recall 功能 v1.4 已整体废弃、无任何消费方
- **位置**：`pandapal/local/run_local.py:520-525`、`pandaren/behavior/context_window_budget.py:32`
- **建议**：默认改 0.0；`context_budget()` 未传时自动置 0
- **优先级**：P1 ｜ **工作量**：小

### G5 · `tool_schema` 配额改为绝对值 8,000，并定义 MCP 子预算
- **现象**：实测 3,374，规范给 24,000（高估 7.1x）；10,000 配额下利用率仅 34%
- **位置**：`run_local.py:520-525`、`pandaren/tool/exposure/budget.py`
- **建议**：工具配额改绝对值 8,000（留 2.4x 余量给 MCP）；MCP 子预算单独封顶
- **优先级**：P1 ｜ **工作量**：小

### G6 · MCP `inputSchema` 加体积校验 + 强制 DEFERRED
- **现象**：`_safe_schema` 原样透传；`tier` 可配 ALWAYS；体积无上限
- **位置**：`pandaren/mcp/tool_adapter.py:125-134, 178-183`
- **建议**：① 单 server 工具数上限；② 单个 `inputSchema` 超过阈值则降级为"仅名字+描述"；③ 默认强制 DEFERRED
- **优先级**：P1 ｜ **工作量**：中

### G7 · 修 `ToolBudget.enforce` 的下限语义
- **现象**：`len(schemas) > max_always_count(15)` 是"总数下限"，导致**总长度 ≤ 15 时预算完全失效**（当前 14，已在失效边缘）
- **位置**：`pandaren/tool/exposure/budget.py:62-64`
- **建议**：下限只保护 ALWAYS 段；或让 `max_always_count` 可配并对超限直接报错（而非静默不裁）
- **优先级**：P1 ｜ **工作量**：小

### G8 · `RESERVED` 改为实测值
- **现象**：实测 512，规范给 `0.08 T` = 4,800 / 6,360（高估 9.4~12.4x）
- **位置**：`pandaren/memory/constants.py:28`、`pandapal/local/llm_policies.py:59`
- **建议**：改为常量 ~1,000（留 2x 余量）；或直接由 `LLMDropSummarizer.max_summary_tokens` 派生
- **优先级**：P1 ｜ **工作量**：小

### G9 · `REINJECT` 未启用却占预算
- **现象**：pandapal 未注入 `post_compact_sources`，实测 0；规范给 `min(0.15 T, 60,000)`
- **位置**：`run_local.py:534-568`、`pandaren/memory/constants.py:78`
- **建议**：未启用时置 0；启用后按各 source 上限实测值分配。同时把 `DEFAULT_POST_COMPACT_TOKEN_BUDGET = 50_000` 这个危险默认值下调
- **优先级**：P2 ｜ **工作量**：小

### G10 · 修 `TOOL_RESULT_CAP` 截断口径（中文越截越大）
- **现象**：中文 64,000 → 64,015（3.2x，反向增长）；代码 32,527（1.6x）
- **位置**：`pandaren/memory/compaction/micro_compact.py:104-108`
- **建议**：粗切后用**同一个 estimator 复估**，超限则继续缩，直到 ≤ cap；或二分反解字符数
- **优先级**：P0 ｜ **工作量**：小

### G11 · 统一估算口径（当前至少 5 套）
- **现象**：真实 BPE / `byte/4` / `chars/4` / `tokens×4字符` 并存，同一批数据差 22.5%~185%
- **位置**：`memory/protocols.py`、`tool/exposure/budget.py:68-83`、`engine/loop.py:214`、`micro_compact.py:105`
- **建议**：所有"判据 + 裁剪"统一走注入的 `token_estimator`；`ToolBudget` 改为接收 estimator
- **优先级**：P0 ｜ **工作量**：中

### G12 · 测量台接入 CI 作为红绿灯
- **现象**：`system_prompt[coding] ≤ system_prompt slot` 这条**当前就已失败**，但没人知道
- **位置**：`scripts/measure_context_budget.py`
- **建议**：加断言「各项实占 ≤ 对应配额」，超标即 CI 红
- **优先级**：P1 ｜ **工作量**：小

### G13 · `MIN_KEEP_RATIO` 需压测校准
- **现象**：现值 0.12，无任何依据；它决定"压缩后 Agent 记得住多少"
- **位置**：`pandaren/memory/constants.py:39`
- **建议**：真实对话压测，观察「保留量 vs 失忆率」，而不是靠算
- **优先级**：P1 ｜ **工作量**：大（需压测框架）

### G14 · skill/agent 摘要预算与配置脱钩
- **现象**：`engine/loop.py:199,203` 调用 `build_skill_summaries()` / `build_agent_summaries()` **不传 `context_window`** → 恒用默认 128,000 × 1% = 1,280（与 app 的 100,000 不一致）
- **位置**：`pandaren/engine/loop.py:199-205`
- **建议**：补传 `context_window`
- **优先级**：P2 ｜ **工作量**：小

### G15 · `DEFAULT_POST_COMPACT_TOKEN_BUDGET = 50_000` 是危险默认值
- **现象**：阈值 65,000 时回注占 77%，一旦启用回注就会把总量顶回阈值之上 → `context_overflow` 停机
- **位置**：`pandaren/memory/constants.py:78`
- **建议**：下调到阈值的 10~15%，或强制由 builder 派生
- **优先级**：P0（一旦启用回注即触发） ｜ **工作量**：小

---

## 5. 建议的配置重分配（CW = 100,000）✅ 已落地（B1）

> 实测验证：`scripts/measure_context_budget.py --check` 退出码 0。
> `compact_threshold` 由 **65,000 → 68,000**，治理前被闲置的 **14,036** token 被回收。

| slot | config 现值 | 实测占用 | **建议值** | 理由 |
|---|---|---|---|---|
| `system_prompt` | 15,000 | **17,590** | **24,000** | 现值已被突破；留余量给更大的工作区 `PANDAPAL.md` |
| `tool_schema` | 10,000 | **3,374** | **8,000** | 留 2.4x 余量给 MCP |
| `conversation` | 65,000 | — | **68,000** | 由余量自动得到 |
| `recall` | 10,000（废弃） | 0 | **0** | 回收 |
| 合计 | 100,000 | 20,964 | 100,000 | — |

换算到 1M 规格（CW = 600,000）：

```
conversation = 600,000 − 24,000 − 8,000 = 568,000
T = 568,000 − BUFFER(5,000) = 563,000
```

> 与规范里按比例推出的 `T = 555,000` 只差 **1.4%**，但**构成完全不同**
> （规范给 system 16,000 + tool 24,000；实测需要 **24,000 + 8,000**）。
> **总数碰巧接近，单项全错。**

---

## 6. 未测到的项（诚实清单）

| 项 | 原因 | 补齐方式 |
|---|---|---|
| `REINJECT` | 未注入 `post_compact_sources`，分子恒为 0 | 启用后重跑测量台 |
| 子 Agent 摘要（41 token） | `SubAgentBlueprint` 的 `name`/`description` 字段取值偏空，**实际偏低估** | 修正字段名后重测 |
| MCP 真实 schema 体积 | 本仓库无 MCP server 配置 | 接入真实 MCP server 后测 |
| `MIN_KEEP_RATIO` 合理性 | 不是"测量"能回答的 | 压测框架（G13） |
| `PANDAPAL.md` 的分布 | 只测了本仓库自身（33,259 字符） | 采集真实用户工作区样本，取 min/median/max |

---

## 7. 如何复现

```bash
cd /Users/jkricky/PycharmProjects/PandapalBuddy
.venv/bin/python scripts/measure_context_budget.py
```

输出 A~G 七节：system_prompt / static_context / tool_schema / 固定开销对照 / RESERVED / 截断效应 / ToolBudget 裁剪行为。
所有数字由 `cl100k_base` 真实编码器计数。

---

## 附录 · 原始测量输出要点

```
A. system_prompt
   PROMPTS['coding']                3,657 字符    2,977 token
   PROMPTS['office']                1,917 字符    1,680 token
   env_block                          266 字符      169 token
   PANDAPAL.md                     33,259 字符   14,045 token
   CODING_RULES.md / soul.md / OFFICE_RULES.md   缺失
   system_prompt[coding]           37,207 字符   17,203 token
   system_prompt[office]            2,184 字符    1,849 token

B. static_context
   build_skill_summaries()            851 字符      346 token（4 个 skill）
   build_agent_summaries()            317 字符       41 token（2 个，偏低估）

C. tool_schema（13 个工具）
   单个 schema 合计                                3,374 token
   真实 tools=[...] payload                        3,388 token
   平均 / 工具                                       260 token

D. 固定开销
   coding                                          20,964 token  ← 配额 25,000，0.84x
   office                                           5,610 token
   system_prompt[coding] / system 配额               1.15x  ❌

E. RESERVED
   ModelSettings(max_tokens=512)

F. TOOL_RESULT_CAP（cap=20,000）
   英文   80,001 → 17,749  ✅
   代码  110,000 → 32,527  ❌ 1.6x
   中文   64,000 → 64,015  ❌ 3.2x（反向增长）

G. ToolBudget.enforce（预算 10,000，max_always_count=15）
   13 个 → 2,614（其口径）→ 裁 0 个
   23 个 → 3,674（其口径）→ 裁 0 个
   真 tokenizer 3,374 vs ToolBudget 2,614 → 差 22.5%
```
