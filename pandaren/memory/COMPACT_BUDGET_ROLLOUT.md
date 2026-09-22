# 上下文预算治理 · 落地实施方案（Rollout Plan）

- 状态：**执行中**（B1 / B2 / B3 已落地，B4 待做）
- 依据：`COMPACT_BUDGET_AUDIT.md`（实测体检报告，G1~G15）
- 规范：`COMPACT_THRESHOLD_SPEC.md`（§0 实测基线、§5.2 实测修正参数表）
- 可视化：`COMPACT_THRESHOLD_MAP.html`（口径切换）

---

## 0. 总目标

把"按比例拍"的 token 预算，换成**实测绝对值 + 比例（仅在缩放项上）**，并修掉实测暴露的 3 个真 bug。

### 非目标（本方案不做）

- 不改 `WindowedKeepPolicy` 的切分算法本身（`windowed.py:138-158` 逻辑正确）
- 不引入 `model_id → context_window` 映射进 SDK（SDK 边界保持）
- 不恢复 recall / 跨 session 召回
- 不动 `Memory._FROZEN_ATTRS` 冻结机制与 `ContextWindowBudget` 不可变语义

---

## 1. 批次划分

| 批次 | 内容 | 风险 | 可独立上线 |
|---|---|---|---|
| **B1** | 配置与常量修正（G1 / G4 / G5 / G8 / G15） | 低 | ✅ |
| **B2** | P0 真 bug 修复（G2 / G10）+ G14 | 低 | ✅ |
| **B3** | 估算口径统一（G11）+ 测量台接 CI（G12） | 中 | ✅ |
| **B4** | MCP 封顶（G6）+ ToolBudget 语义（G7）+ 片段预算（G3）+ MIN_KEEP 压测（G13） | 中~高 | 分步 |

---

## 2. B1 · 配置与常量修正

### B1-1（G4）`recall_ratio` 默认置 0

| 项 | 内容 |
|---|---|
| 文件 | `pandaren/behavior/context_window_budget.py:32`、`pandapal/local/run_local.py` |
| 改动 | `DEFAULT_RECALL_RATIO: 0.10 → 0.00`；app 显式传 `recall_ratio=0.0` |
| 依据 | recall（跨 session 召回）v1.4 已整体废弃，无任何消费方；实测 10,000 配额全空置 |
| 验收 | `test_cwb_05` 断言更新为 `recall_tokens == 0`；测量台 `recall` 行显示 0 |
| 回滚 | 单行还原 |

### B1-2（G1 + G5）固定尺寸槽位改绝对值

| 项 | 内容 |
|---|---|
| 文件 | `context_window_budget.py`、`pandaren/builder.py`、`pandapal/local/run_local.py` |
| 改动 | `ContextWindowBudget` 新增 `system_prompt_tokens_abs` / `tool_schema_tokens_abs`；给了绝对值则**优先于 ratio**，剩余自动归 conversation。`builder.context_budget()` 透传（含子 Agent 继承）。app 改为 `system_prompt_tokens_abs=24_000`、`tool_schema_tokens_abs=8_000` |
| 依据 | 实测 coding 需 17,590 / office 只需 2,236（差 9.3 倍）；tool 实测 3,374（原配额 10,000 闲置 66%） |
| 验收 | 测量台 D 节：`system_prompt` 不再超配额；`recall` 行 0；`tool_schema` 利用率 ≥ 40% |
| 回滚 | app 侧去掉 `_tokens_abs` 参数即回到 ratio 行为 |

**绝对槽位的语义**（新增不变式 I7/I8）：

```
I7: 若给了 abs，则 slot = abs（不再 floor(CW × ratio)）
I8: conversation 自动吸收剩余 = CW − Σ(其他 slot)，必须 > 0
```

### B1-3（G8）`RESERVED` 改实测常量

| 项 | 内容 |
|---|---|
| 文件 | `pandaren/memory/constants.py`、`pandaren/memory/memory.py` |
| 改动 | 新增 `DEFAULT_RESERVED_SUMMARY_TOKENS = 512`；`Memory` 用它替代 `0.08T` |
| 依据 | 实测 `LLMDropSummarizer` 实际下发 `ModelSettings(max_tokens=512)`；规范给的 4,800/6,360 高估 9.4~12.4x |
| 验收 | 压缩后总量核算里摘要预留 = 512 |

### B1-4（G15）`DEFAULT_POST_COMPACT_TOKEN_BUDGET` 下调

| 项 | 内容 |
|---|---|
| 文件 | `pandaren/memory/constants.py:78` |
| 改动 | `50_000 → 8_000` |
| 依据 | 阈值 65,000 时回注占 77%，一旦启用回注就把总量顶回阈值之上 → `context_overflow` 停机 |
| 验收 | `Memory.__init__` 新增不变式告警：`RESERVED + REINJECT > 0.3 × threshold` 时 warning |

### B1-5（R4/R5）`target_tokens` 显式预留

| 项 | 内容 |
|---|---|
| 文件 | `pandaren/memory/memory.py:696-704` |
| 改动 | `target_tokens` 减去 `RESERVED` 与 `REINJECT` 预算 |
| 依据 | 原公式只扣了**压缩前**的旧 attachments，没扣**即将回注**的量 → 压缩后又被顶超阈值 |
| 验收 | 新增测试：开启 `drop_summarizer` + `post_compact_sources` 后，压缩后总量仍 < 阈值 |

---

### B1-6（R1 + R2）跟随模型配置：模型 → 上限 → 档位比例表

| 项 | 内容 |
|---|---|
| 新增 | `pandapal/config/llm/model_context_windows.toml`（模型映射 + 4 个档位） |
| 新增 | `pandapal/config/llm/context_window_resolver.py`（解析器 + CLI） |
| 改动 | `run_local.py`：`resolve_budget(model_id)` → `context_budget(**budget.to_builder_kwargs())` |
| 依据 | 需求：预算必须**跟随模型配置**，且各字段 = **比例 × 模型上下文上限**，保持动态性 |
| 验收 | `--table` 输出档位表；未知模型回落 128K + WARNING；`test_context_window_resolver.py` 8 项通过 |

**默认档位表**（`--table` 实输出）：

| 档位 | M 上限 | CW/M | system 比例 | system | tool 比例 | tool | recall | conversation |
|---|---|---|---|---|---|---|---|---|
| `small` | ≤ 160,000 | 0.80 | 0.172 | 22,016 | 0.078 | 9,984 | 0 | 70,400 |
| `medium` | ≤ 300,000 | 0.80 | 0.110 | 22,000 | 0.050 | 10,000 | 0 | 128,000 |
| `large` | ≤ 600,000 | 0.80 | 0.060 | 24,000 | 0.020 | 8,000 | 0 | 288,000 |
| `huge` | ≤ ∞（1M 级） | 0.60 | 0.024 | 24,000 | 0.008 | 8,000 | 0 | 568,000 |

> ⚠️ **比例随档位递减**是刻意的，不是笔误：`system_prompt` / `tool_schema` 的真实占用
> 不随窗口增长（实测 coding 17,590 / office 2,236；13 个工具 schema 3,374）。
> 若比例恒定 0.15，1M 模型会拿到 150,000 的 system 配额（实测只需 2 万）——
> 那就是原始 spec 的病根。**递减的比例 + 比例 × M 的公式 = 动态且不失控。**

**计算链**：

```
model_id ──(exact / pattern / default 三级匹配)──▶ model_max_context
         ──(落入 max_context 区间)──────────────▶ tier
CW   = floor(M × context_window_ratio)
slot = max(floor(M × ratio), floor)          ← floor 兜底，防小模型被比例压死
conv = CW − system − tool − recall           ← 自动吸收剩余
```

**⚠️ 映射表数据必须核实（G16）**：表是静态数据，厂商"稳定别名"背后的服务端版本会漂移。
典型：`deepseek-chat` 在 V3.x 是 **128K**、在 V4 是 **1M**。
因此：

- 表中**只保留已核实条目**（DeepSeek V3/V4 按版本分开、`*1m*` 自述名、Claude 200K）
- 未核实条目（`gpt-4o*` / `gemini-*` / `qwen*` / `glm-4*` / `moonshot*`）**已全部删除**
- 未声明 → `[default] 128K` + WARNING
- 应急接口：`PANDAPAL_MODEL_MAX_CONTEXT=1000000`（最高优先级，改完即生效）
- BYOK 场景推荐在 `[models]` 里**显式声明**自己在用的模型 id

**1M 模型的解析结果**（实测验证）：

```
model=vendor-1m-flagship [pattern:*1m*] tier=huge M=1,000,000
  → CW=600,000 (system=24,000 tool=8,000 recall=0 conversation=568,000)
  → T 触发阈值 = 568,000 − BUFFER(5,000) = 563,000
```

---

## 3. B2 · P0 真 bug 修复

### B2-1（G10）`TOOL_RESULT_CAP` 截断改为按 estimator 收敛

| 项 | 内容 |
|---|---|
| 文件 | `pandaren/memory/compaction/micro_compact.py:89-115` |
| 问题 | 判定用真实 tokenizer、裁剪用 `CHARS_PER_TOKEN = 4.0`。实测中文 64,000 → **64,015**（越截越大）、代码 → 32,527（1.6x） |
| 改动 | 粗切 → 用**同一个 estimator** 复估 → 二分收敛，直到 `正文 + 截断标记 ≤ cap` |
| 验收 | 新增测试：中文 / 英文 / 代码三种内容截断后真实 token 均 ≤ cap |
| 回滚 | 函数级还原 |

### B2-2（G2）`static_context` 截断判据改用注入的 estimator

| 项 | 内容 |
|---|---|
| 文件 | `pandaren/engine/loop.py:213-235`、`pandaren/memory/memory.py`（新增公开方法） |
| 问题 | 判据用 `len(text)/4`，对中文混排低估 1.85x（coding）/ 3.39x（office）→ 配额超支**永远发现不了** |
| 改动 | `Memory` 新增 `estimate_text()` / `truncate_text_to_tokens()`（与压缩判据同一把尺子）；`loop.py` 改用它们 |
| 验收 | 新增测试：中文 system_prompt 超配额时能正确告警/截断 |

### B2-3（G14）skill / agent 摘要预算补传 `context_window`

| 项 | 内容 |
|---|---|
| 文件 | `pandaren/engine/loop.py:199、203` |
| 问题 | 不传参 → 恒用默认 `128_000 × 1% = 1,280`，与 app 的配置脱钩 |
| 改动 | 补传 `self._context_window_budget.context_window` |
| 验收 | 配置变化时摘要预算随之变化 |

---

## 4. B3 · 估算口径统一（✅ 已完成）

### B3-1（G11）消灭多套口径

现状（实测同一批 13 个 schema 差 22.5%）：

| 位置 | 口径 | 动作 |
|---|---|---|
| `memory/protocols.py` `CharBasedTokenEstimator` | `chars/4` | 保留为**零依赖兜底**，但标注"仅用于无 tokenizer 场景" |
| `tool/exposure/budget.py:68-83` | `utf-8 bytes/4` | ✅ 已改为**接收注入的 estimator**（`builder.py._build_tool_layer` 注入；未注入时回落 bytes/4） |
| `engine/loop.py:214` | `chars/4` | ✅ B2-2 已修 |
| `micro_compact.py:105` | `tokens × 4 字符` | ✅ B2-1 已修 |
| `memory/estimators.py` `TiktokenEstimator` | 真实 BPE | 基准 |

验收：全仓库 `grep` 后只剩 `TiktokenEstimator` + `CharBasedTokenEstimator`（显式兜底）+ `ToolBudget` 改为注入。

### B3-2（G12）测量台接 CI

| 项 | 内容 |
|---|---|
| 文件 | `scripts/measure_context_budget.py` |
| 改动 | 新增 `--check` 模式：断言「各项实占 ≤ 对应配额」，失败退出码 1 |
| 当前状态 | `system_prompt[coding] ≤ slot` **当前就已失败** —— 加了断言 CI 立刻变红，正好当治理进度条 |
| 验收 | `python scripts/measure_context_budget.py --check` 在 B1 完成后退出码 0 |

---

## 5. B4 · 待做（每项独立）

| 项 | 内容 | 关键风险 |
|---|---|---|
| **G3** | 为 `FragmentSpec` 增加 `max_tokens`，超限截断单个片段 | 截断 `PANDAPAL.md` 可能丢失关键指令，需按段落边界截断 |
| **G6** | MCP `inputSchema` 体积校验 + 默认强制 DEFERRED + 单 server 工具数上限 | 第三方 server 兼容性 |
| **G7** | `ToolBudget.enforce` 下限语义（当前总长 ≤ 15 时预算整体失效） | 需确认 `max_always_count` 的设计意图 |
| **G13** | `MIN_KEEP_RATIO` 压测校准（不是测量能回答的） | 需压测框架 + 失忆判定指标 |
| **G9** | `REINJECT` 启用时的预算分配 | 依赖 G6 |

---

## 6. 总验收标准

### 6.1 自动化

```bash
.venv/bin/python scripts/measure_context_budget.py --check   # 全部实占 ≤ 配额
.venv/bin/python -m pytest pandaren/memory/tests -q
.venv/bin/python -m pytest pandaren/behavior/tests -q
.venv/bin/python -m pytest pandaren/engine/tests -q
```

### 6.2 逐项对照（B1 + B2 完成后应满足）

| 指标 | 治理前 | 目标 |
|---|---|---|
| `system_prompt[coding]` vs 配额 | **1.15x ❌** | ≤ 1.0 |
| `tool_schema` 利用率 | 34% | ≥ 40% |
| `recall` 占用 | 10,000（全空） | **0** |
| `RESERVED` 设定值 | 5,200 | **512** |
| `REINJECT` 设定值 | 50,000 | **8,000**（且未启用时不计入） |
| 被闲置配额 | **14,423** | ≤ 5,000 |
| 中文工具结果截断 | **64,000 → 64,015 ❌** | ≤ cap |
| `static_context` 判据低估 | **1.85x** | 1.0x |

### 6.3 人工验证

- `COMPACT_THRESHOLD_MAP.html` 切到「实测修正」口径，面板 ⓿ 不再有红色项
- 真实会话跑一轮，日志中无 `context_overflow` 停机

---

## 7. 变更清单（文件级）

| 文件 | 批次 | 改动 |
|---|---|---|
| `pandaren/memory/constants.py` | B1 | 新增 `DEFAULT_RESERVED_SUMMARY_TOKENS`；`DEFAULT_POST_COMPACT_TOKEN_BUDGET` 下调 |
| `pandaren/memory/memory.py` | B1/B2 | `target_tokens` 预留摘要+回注；新增 `estimate_text` / `truncate_text_to_tokens`；不变式告警 |
| `pandaren/memory/compaction/micro_compact.py` | B2 | 截断按 estimator 二分收敛 |
| `pandaren/behavior/context_window_budget.py` | B1 | 绝对槽位支持；`DEFAULT_RECALL_RATIO → 0` |
| `pandaren/builder.py` | B1 | `context_budget()` 透传绝对值（含子 Agent 继承） |
| `pandaren/engine/loop.py` | B2 | 截断判据改用 estimator；摘要预算补传 `context_window` |
| **`pandapal/config/llm/model_context_windows.toml`** | B1 | **新增**：模型 → 上下文上限映射 + 4 档位比例表 |
| **`pandapal/config/llm/context_window_resolver.py`** | B1 | **新增**：`resolve_budget()` / `describe_table()` + CLI |
| **`pandapal/config/tests/test_context_window_resolver.py`** | B1 | **新增**：CWR-1~8（模型匹配 / 档位比例 / 字段配额 / builder 契约） |
| `pandapal/local/run_local.py` | B1 | 改为 `resolve_budget(model_id)` 驱动（跟随模型配置） |
| `pandaren/behavior/tests/test_behavior_guards.py` | B1 | 更新 CWB-05 默认值断言 |
| `scripts/measure_context_budget.py` | B3 | 新增 `--check` 断言模式 |

---

## 8. 执行记录

| 批次 | 状态 | 完成时间 | 验证结果 |
|---|---|---|---|
| **B1** | ✅ **已完成** | 2026-09-22 | `--check` 退出码 0；`test_cwb_06/07/08` 新增并通过 |
| **B2** | ✅ **已完成** | 2026-09-22 | 截断实测：英文 80,001→20,000、代码 110,000→19,999、中文 64,000→**19,999**（原 64,015） |
| **B3** | ✅ **已完成** | 2026-09-22 | `--check` 已接 CI（`.github/workflows/lint.yml` → `context-budget` job）；`ToolBudget` 改为可注入 estimator |
| **B5（新增）** | ✅ **已完成** | 2026-09-22 | 阈值比例派生收口（见下） |
| B4 | ⬜ 待做 | — | G3 / G6 / G7 / G13 |

### B5 · 阈值比例派生收口（R1~R5）

治理前：`min/max_keep_tokens`、单条工具结果上限、restore 预算全是**写死的绝对值**，
不随 `context_window` 缩放——1M 模型下压缩后最多只留 40K（丢掉 93%），
small 档 restore 一次灌 64K（几乎等于整个 conversation 配额）。

| 项 | 改动 | 落点 |
|---|---|---|
| R5 | restore 预算 → `self._compact_threshold` | `memory.py` `init_from_restore` |
| R3 | `BUFFER` 接入：`T = conversation_slot − buffer` | `builder.py` `_build_memory_factory` |
| R2 | `WindowedKeepPolicy(min=0.12T, max=0.45T)` 按阈值派生 | `constants.derive_keep_window` + `builder.py` |
| R1 | 单条工具结果上限 `min(0.15T, 30K)`（下限 8K） | `constants.derive_single_result_max_tokens` |
| R4 | `DEFAULT_RESERVED_OUTPUT_TOKENS` 接入 I1 校验：`CW + 输出预留 ≤ 0.9 × 模型上限` | `context_window_resolver.resolve_budget` |

派生效果（实测，见 `pandaren/memory/tests/test_derived_budget.py`）：

| 档位 | CW | conversation | T | min_keep | max_keep | 工具上限 |
|---|---|---|---|---|---|---|
| small（128K） | 102,400 | 70,400 | 65,400 | 7,848 | 29,430 | 9,810 |
| medium（200K） | 160,000 | 128,000 | 123,000 | 14,760 | 55,350 | 18,450 |
| huge（1M） | 600,000 | 568,000 | 563,000 | **67,560** | **253,350** | 30,000 |

> 1M 档 `MIN_KEEP` 8,000 → 67,560（**8.4x**）。`MIN_KEEP_RATIO` 的最终取值靠 B4-G13 压测校准。

### B1 + B2 实际改动

| 文件 | 改动 |
|---|---|
| `pandaren/behavior/context_window_budget.py` | 新增 `system_prompt_tokens_abs` / `tool_schema_tokens_abs`（绝对槽位，conversation 吸收剩余）；`DEFAULT_RECALL_RATIO 0.10 → 0.00` |
| `pandaren/builder.py` | `context_budget()` 透传绝对槽位；子 Agent 继承时一并传递 |
| **`pandapal/config/llm/model_context_windows.toml`**（新增） | 模型 → `model_max_context`（exact/pattern/default）+ 4 档位比例表（`small`/`medium`/`large`/`huge`） |
| **`pandapal/config/llm/context_window_resolver.py`**（新增） | `resolve_model_max_context()` / `resolve_budget()` / `describe_table()` + CLI（`--table` / `--model`） |
| **`pandapal/config/tests/test_context_window_resolver.py`**（新增） | CWR-1~8 |
| `pandapal/local/run_local.py` | 改为 `resolve_budget(default_cred["model_id"])` 驱动；未命中模型回落 + WARNING |
| `pandaren/memory/constants.py` | 新增 `DEFAULT_RESERVED_SUMMARY_TOKENS = 512`；`DEFAULT_POST_COMPACT_TOKEN_BUDGET 50_000 → 8_000` |
| `pandaren/memory/memory.py` | `target_tokens` 预留摘要 + 回注；新增 `estimate_text` / `truncate_text_to_tokens`；固定占用超阈值 30% 时告警 |
| `pandaren/memory/compaction/micro_compact.py` | 截断改为 estimator 二分收敛 |
| `pandaren/engine/loop.py` | static_context 截断判据改用 estimator；摘要预算补传 `context_window` |
| `pandaren/behavior/tests/test_behavior_guards.py` | 更新 CWB-05；新增 CWB-06/07/08 |
| `scripts/measure_context_budget.py` | 新增 `--check` 断言模式 + 治理前后对照表 |

### 治理效果（实测）

| 指标 | 治理前 | 治理后 |
|---|---|---|
| `system_prompt[coding]` vs 配额 | 15,000（实占 17,590）**超 1.17x** | 24,000，实占 17,590 ✅ |
| `tool_schema` 利用率 | 34% | 42%（8,000 配额 / 实占 3,374） |
| `recall` 占用 | 10,000（100% 空置） | **0** |
| `RESERVED` 设定值 | 5,200 | **512** |
| `REINJECT` 设定值 | 50,000 | **8,000**（未启用时预留 0） |
| 被闲置配额 | **14,036** | 0 |
| `compact_threshold` | 65,000 | **68,000** |
| 中文工具结果截断 | 64,000 → **64,015**（越截越大） | 64,000 → **19,999** ✅ |
| `static_context` 判据偏差 | 低估 1.85x | 1.0x |

### 测试基线

```
.venv/bin/python scripts/measure_context_budget.py --check   # 退出码 0 ✅
.venv/bin/python -m pytest -q --deselect pandaren/utils/tests/test_path_utils.py::TestExpandPath::test_absolute_kept
# → 1457 passed, 27 skipped, 14 xfailed ✅
#（deselect 的是 macOS /tmp 符号链接的既有失败，与本次改动无关）
```
