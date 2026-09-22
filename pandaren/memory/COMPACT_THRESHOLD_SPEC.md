# 上下文压缩预算与阈值分配规范（Compact Threshold Spec）

- 状态：待评审
- 范围：`pandaren/memory/`、`pandaren/behavior/context_window_budget.py`、`pandaren/builder.py`、`pandapal/local/run_local.py`
- 替换：现行"绝对值硬编码 + 阈值孤岛"方案
- **实测依据：`COMPACT_BUDGET_AUDIT.md`**（2026-09-22，cl100k_base 真实计数）

---

## 0. 实测基线（先读这一节）

本文档的数值分两类，**不要混用**：

| 类别 | 成员 | 定值方式 | 依据 |
|---|---|---|---|
| **实测绝对值** | `SYS_ACTUAL` / `SYS_CAP` / `TOOL_SCHEMA_CAP` / `RESERVED` / `REINJECT` / `TOOL_RESULT_CAP` | 真实 tokenizer 实测 + 留余量 | `COMPACT_BUDGET_AUDIT.md` §1 |
| **比例值** | `T` / `TARGET` / `KEEP_CAP` / `MIN_KEEP` / `headroom` | 按 T 的比例（这层保留） | 设计目标，需压测校准 |

### 0.0 预算来源：模型 → 上限 → 档位比例表（已落地）

本节所有数值**不再写死在应用层**，而是由
`pandapal/config/llm/model_context_windows.toml` + `context_window_resolver.py` 计算：

```
model_id ──(exact/pattern/default)──▶ model_max_context ──▶ tier
CW   = floor(M × context_window_ratio)
slot = max(floor(M × ratio), floor)      ← 固定槽位：比例 × M，带兜底下限
conv = CW − system − tool − recall        ← 自动吸收剩余
```

| 档位 | M 上限 | CW/M | system 比例 | system | tool 比例 | tool | conversation |
|---|---|---|---|---|---|---|---|
| `small` | ≤ 160,000 | 0.80 | 0.172 | 22,016 | 0.078 | 9,984 | 70,400 |
| `medium` | ≤ 300,000 | 0.80 | 0.110 | 22,000 | 0.050 | 10,000 | 128,000 |
| `large` | ≤ 600,000 | 0.80 | 0.060 | 24,000 | 0.020 | 8,000 | 288,000 |
| `huge` | ≤ ∞（1M 级） | 0.60 | 0.024 | 24,000 | 0.008 | 8,000 | 568,000 |

**⚠️ 比例随档位递减是设计而非笔误**：`system_prompt` / `tool_schema` 的真实占用不随窗口
增长（实测 coding 17,590 / office 2,236；13 个工具 3,374）。固定比例 0.15 在 1M 模型下会
给出 150,000 的 system 配额（实测只需 2 万）——这正是原始 spec 的病根。

```bash
.venv/bin/python -m pandapal.config.llm.context_window_resolver --table
.venv/bin/python -m pandapal.config.llm.context_window_resolver --model <model-id>
```

### 0.1 实测值（1M 模型 · 平衡档 CW=600,000）

| 项 | 实测 | 建议预算 | 说明 |
|---|---|---|---|
| `system_prompt[coding]` | **17,203** | `SYS_CAP = 24,000`（绝对值） | 其中 `PANDAPAL.md` **14,045（82%）** |
| `system_prompt[office]` | **1,849** | 同上（共用槽位） | 同一配额，实际只用 12% |
| `static_context`（技能 + 子 Agent 摘要） | **387** | 含在 `SYS_ACTUAL` 内 | 拼进 system message 末尾 |
| `tool_schema`（13 个工具） | **3,374** | `TOOL_SCHEMA_CAP = 8,000`（绝对值） | 平均 260 / 工具；MCP 需另封顶 |
| `RESERVED`（摘要输出） | **512** | `512`（常量） | `ModelSettings(max_tokens=512)` |
| `REINJECT`（回注） | **0** | `0`（未启用） | pandapal 未注入 `post_compact_sources` |
| `TOOL_RESULT_CAP` | **20,000** | `20,000`（常量） | 代码默认值 |

→ `SYS_ACTUAL = 17,203 + 387 = 17,590`（coding）/ `1,849 + 387 = 2,236`（office）。

### 0.2 三条必须记住的实测结论

1. **`system_prompt` 在 coding 模式已超配额**：实测 17,203 vs config 的 `0.15 × 100,000 = 15,000`（**1.15x**）。而截断判据用 `len(text)/4` 只会算出 9,302（**低估 1.85x**），所以程序以为还剩 5,698，**永远不会报警**。
2. **`PANDAPAL.md` 是决定项且随工作区变化**：14,045 / 17,203 = **82%**。`PromptAssembler` 每次 run 按 sha256 热重载 → 换工作区，system_prompt 可从 3K 变到 20K+。**`SYS_ACTUAL` 在原理上不该是常量。**
3. **用 ratio 给固定尺寸槽位配额是错的**：coding 要 17,203、office 只要 1,849，差 **9.3 倍**，一个固定比例不可能同时合适。

### 0.3 配置重分配（CW = 100,000）

| slot | config 现值 | 实测占用 | **建议值** |
|---|---|---|---|
| `system_prompt` | 15,000 ❌ 已超 | **17,590** | **24,000**（绝对值） |
| `tool_schema` | 10,000（闲置 66%） | **3,374** | **8,000**（绝对值） |
| `conversation` | 65,000 | — | **68,000** |
| `recall` | 10,000 ❌ 空置 | 0 | **0** |
| 合计 | 100,000 | 20,964 | 100,000 |

**被闲置的总量 ≈ 14,423 token**，本该划给 conversation。

> ⚠️ 口径说明：§3 全景图与 §5.1 参数表保留**原始 spec 口径**（按比例拍）仅用于对比；
> **§4 公式与 §5.2 参数表已按实测修正**，以此为准。
> 可视化对照见 `COMPACT_THRESHOLD_MAP.html` 的「口径 / system_prompt 模式」切换。

---

## 1. 目的

消除上下文压缩链路中的**四处阈值孤岛**，建立**单一真相源**：

- **会随窗口缩放的量**（T / TARGET / KEEP_CAP / MIN_KEEP）→ 从 `context_window` 派生；
- **固定尺寸槽位**（system / tool_schema / RESERVED / REINJECT / TOOL_RESULT_CAP）→
  用**实测绝对值常量**（见 §0.1），**禁止比例推导**。

| ID | 问题 | 位置 |
|---|---|---|
| P1 | 回注预算 50K ≈ 触发阈值 65K（77%），压缩后极易被顶回阈值之上 → `compact_if_needed` 返回 overflow → Agent 直接停机 | `memory/constants.py:78` vs `run_local.py:521` |
| P2 | 保留窗口 `max_keep_tokens=40_000` / `min_keep_tokens=8_000` 是绝对值，不随窗口缩放 | `memory/constants.py:39,45` |
| P3 | `DEFAULT_COMPACT_BUFFER_TOKENS` / `DEFAULT_RESERVED_OUTPUT_TOKENS` 是**死常量**，注释声称的"阈值 = 配额 − buffer"从未实现 | `memory/constants.py:28,32` |
| P4 | `DEFAULT_RESTORE_TOKEN_BUDGET` 用模块级默认值，不跟随实际配置的阈值 | `memory/memory.py:477` |
| P5 | `recall_ratio` 默认 0.10，但跨 session 召回功能 v1.4 已整体废弃、**无任何消费方**，白占配额且把 ratio 之和顶在 1.00 | `memory/constants.py:100-105` |
| P6 | `skill/registry.py:307`、`sub_agent/registry.py:319` 硬编码 `128_000`，调用方不传参 → 与 app 配置脱钩 | `engine/loop.py:199,203` |
| P7 | 应用层 `context_window=100000` 写死，与用户凭据里的模型无关 | `run_local.py:520-525` |
| P8 | `compact_if_needed` **零测试覆盖** | `memory/tests/` |
| **P9** | **`system_prompt` 在 coding 模式已超配额 1.15x，且截断判据低估 1.85x 导致无法发现** | `engine/loop.py:213-235`、`run_local.py:520-525` |
| **P10** | **同一批数据存在 ≥5 套估算口径**（真实 BPE / `byte/4` / `chars/4` / `tokens×4字符`），最大偏差 185% | `memory/protocols.py`、`tool/exposure/budget.py:68-83`、`engine/loop.py:214`、`micro_compact.py:105` |
| **P11** | **`ToolBudget.enforce` 在总长度 ≤ 15 时整体失效**（当前 13 ALWAYS + search_tools = 14，已在边缘） | `tool/exposure/budget.py:62-64` |
| **P12** | **`TOOL_RESULT_CAP` 截断在中文场景失效且反向**（64,000 → 64,015，越截越大） | `micro_compact.py:104-108` |

---

## 2. 术语与锚点

### 2.1 两个必须区分的概念

| 概念 | 含义 | 谁决定 |
|---|---|---|
| `model_max_context` | 模型硬上限（128K / 200K / 400K / **1M**） | 模型配置映射表 |
| `context_window` (CW) | **单次请求的输入预算** | 档位比例 × `model_max_context` |

> 铁律：CW 是模型上限的**一个比例**，不是等于模型上限。

### 2.2 档位定义：模型窗口的使用比例

| 档位 | 比例 P | 1M 模型 | 400K 模型 | 200K 模型 | 128K 模型 |
|---|---|---|---|---|---|
| 保守 | 0.40 | 400,000 | 160,000 | 80,000 | 51,200 |
| **平衡（默认）** | **0.60** | **600,000** | 240,000 | 120,000 | 76,800 |
| **激进** | **0.80** | **800,000** | 320,000 | 160,000 | 102,400 |

**1M 上下文模型的答案：平衡档 `context_window = 600,000`，激进档 `= 800,000`。**

### 2.3 触发阈值与请求总量不是一回事

`Memory.estimate_tokens()` 只统计 **system + 回注 + 对话历史**（`memory.py:635-642`），
**不含 tool schema、不含模型输出**：

```
真实请求 token = (system + 回注 + 对话历史)   ← 本规范管辖（≤ T）
               + tool_schema                 ← ToolBudget slot 管辖
               + 模型输出                    ← llm_settings(max_tokens) 管辖
```

### 2.4 双尺子原则（大窗口的必备前提）

600K 量级下，纯比例派生会让"固定用途"的预算失控（回注 84K、单条工具结果 84K、
system slot 90K）。因此：

| 类别 | 派生方式 | 实测值（1M 平衡档） | 理由 |
|---|---|---|---|
| **会随窗口增长**：T / TARGET / KEEP_CAP / MIN_KEEP | 按 T 的比例 | T = **563,000** | 对话深度本来就该随窗口增长 |
| **固定用途**：system / tool_schema / REINJECT / RESERVED / TOOL_RESULT_CAP | **绝对值常量**（不再用比例） | **24,000 / 8,000 / 0 / 512 / 20,000** | 用途与窗口无关。**实测证明 `min(比例, 上限)` 仍然错**——见 §0.2 结论 3 |

---

## 3. 分配全景图（L0 → L3）

> ⚠️ **下图是「原始 spec 口径」**（按比例拍，未实测）。
> 实测修正口径的系统提示词实占为 **17,590（coding）/ 2,236（office）**，
> 工具 schema 实占 **3,374**，`RESERVED` 实占 **512**，`REINJECT` 实占 **0**。
> 详见 §0 与 `COMPACT_BUDGET_AUDIT.md`。

以 **1M 模型 · 平衡档（CW=600,000）** 为例（原始 spec 口径）：

```
model_max_context                                                  1,000,000
├── 模型输出预留  max_tokens                                       32,000
├── 安全余量      ≥ 10% × model_max_context                        100,000
└── CW = context_window = floor(1,000,000 × 0.60)                  ██ 600,000 ██
    ├── system_prompt slot   min(0.15CW, 16,000)                   16,000   （上限）
    ├── tool_schema slot     min(0.10CW, 24,000)                   24,000   ★ 不计入 T
    ├── conversation slot    CW − sys − tool                       560,000
    │   ├── BUFFER 触发提前量                                       5,000
    │   └── T = 压缩触发阈值                                        ██ 555,000 ██
    │       ├── headroom 0.30T                                    166,500
    │       │   ├── REINJECT 回注总预算 min(0.15T, 60,000)         60,000
    │       │   │   ├── files  min(0.08T, 40,000)（3 个，单个 ≤20,000） 40,000
    │       │   │   ├── skills min(0.04T, 12,000)（单个 ≤6,000）    12,000
    │       │   │   └── plan   min(0.03T,  8,000)                   8,000
    │       │   └── RESERVED 摘要输出预留 min(0.08T, 32,000)       32,000
    │       └── TARGET 0.70T                                      388,500
    │           ├── SYS_ACTUAL  system 实际估算                     ~6,000
    │           ├── OLD_ATT     压缩前旧回注                       0（首次压缩）
    │           └── KEEP_BUDGET → 传给 split()                    290,500
    │               ├── MIN_KEEP min(0.12T, KEEP_CAP)（下限）      66,600
    │               └── KEEP_CAP  min(0.45T, budget)（硬上限）     249,750
    └── 未分配余量                                                  0（全部划入 conversation）

入口侧子限额（不属于 T）
└── 单条工具结果截断上限  min(0.15T, 30,000)                       30,000
```

激进档（CW=800,000）同构，数值见 §5。

---

## 4. 计算公式（唯一真相源）

```python
# ── L0 ──
CW              = floor(model_max_context * PROFILE_RATIO[profile])   # 0.40 / 0.60 / 0.80
assert CW + max_tokens <= model_max_context * 0.9                     # I1

# ── L1 ──（固定尺寸 slot = 绝对值，实测校准；见 §0.1）
SYS_CAP         = SYS_CAP_ABS          # 24,000   实测 coding 需 17,590（含 static_context）
TOOL_SCHEMA_CAP = TOOL_SCHEMA_CAP_ABS  #  8,000   实测 13 个工具 = 3,374
CONV            = CW - SYS_CAP - TOOL_SCHEMA_CAP

# ── L2 ──
T               = CONV - BUFFER                               # BUFFER = 5,000
TARGET          = floor(T * COMPACT_TARGET_RATIO)             # 0.70
REINJECT        = REINJECT_ABS         #      0   未启用；启用后按各 source 上限实测分配
RESERVED        = RESERVED_ABS         #    512   实测 ModelSettings(max_tokens=512)
assert RESERVED + REINJECT <= T - TARGET + RESERVED           # I3

# ── L3 ──
KEEP_BUDGET     = TARGET - SYS_ACTUAL - OLD_ATT - RESERVED - REINJECT
KEEP_CAP        = min(floor(T * MAX_KEEP_RATIO), KEEP_BUDGET)  # 0.45
MIN_KEEP        = min(floor(T * MIN_KEEP_RATIO), KEEP_CAP)     # 0.12
# ↑ 不用写死的绝对上限：那会把大窗口的长上下文又丢掉。KEEP_CAP 已足够约束。
assert MIN_KEEP <= KEEP_CAP <= KEEP_BUDGET                     # I4
assert T >= MIN_KEEP + SYS_ACTUAL                              # I5

# ── 入口侧子限额 ──
TOOL_RESULT_CAP = TOOL_RESULT_CAP_ABS  # 20,000（代码默认值）
# ⚠️ P12：裁剪必须与判定用同一把尺子，否则中文场景会"越截越大"
```

### 不变式

| ID | 不变式 | 违反后果 |
|---|---|---|
| I1 | `CW + max_tokens ≤ model_max_context × 0.9` | API 400 / 超限 |
| I2 | `Σ slot 占比 ≤ CW` | 预算超配 |
| I3 | `REINJECT + RESERVED ≤ T − TARGET` | 压缩后立即再次触发 |
| I4 | `MIN_KEEP ≤ KEEP_CAP ≤ KEEP_BUDGET` | 策略参数非法 |
| I5 | `T ≥ MIN_KEEP + SYS_ACTUAL` | 阈值过小，压缩无意义 |
| I6 | 档位门槛：`model_max_context ≥ CW / 0.9 且 max_tokens ≤ model_max_context − CW` | 自动降级到小档位 + warning |

违反 I1~I5：`logger.warning` + **自动收敛到最近合法值**，不拒绝启动（E4/E5）。

---

## 5. 档位参数表（1M 模型，最终值）

| 参数 | 公式 | 保守 (P=0.40) | **平衡 (P=0.60)** | **激进 (P=0.80)** |
|---|---|---|---|---|
| `context_window` | `floor(1M × P)` | 400,000 | **600,000** | **800,000** |
| `system_prompt` slot | `min(0.15CW, 16K)` | 16,000 | 16,000 | 16,000 |
| `tool_schema` slot | `min(0.10CW, 24K)` | 24,000 | 24,000 | 24,000 |
| `conversation` slot | `CW − 40,000` | 360,000 | 560,000 | 760,000 |
| BUFFER | 固定 | 5,000 | 5,000 | 5,000 |
| **T 触发阈值** | `CONV − BUFFER` | **355,000** | **555,000** | **755,000** |
| TARGET | `0.70T` | 248,500 | 388,500 | 528,500 |
| REINJECT | `min(0.15T, 60K)` | 53,250 | **60,000**(cap) | **60,000**(cap) |
| RESERVED | `min(0.08T, 32K)` | 28,400 | **32,000**(cap) | **32,000**(cap) |
| SYS_ACTUAL | 实测 | ~6,000 | ~6,000 | ~8,000 |
| KEEP_BUDGET | 见 §4 | 160,850 | 290,500 | 428,500 |
| KEEP_CAP | `min(0.45T, budget)` | 159,750 | 249,750 | 339,750 |
| MIN_KEEP | `min(0.12T, KEEP_CAP)` | 42,600 | 66,600 | **90,600** |
| TOOL_RESULT_CAP | `min(0.15T, 30K)` | 30,000(cap) | 30,000(cap) | 30,000(cap) |
| 回注·files | `min(0.08T, 40K)`，3 个 | 28,400 | 40,000 | 40,000 |
| 回注·skills | `min(0.04T, 12K)` | 12,000 | 12,000 | 12,000 |
| 回注·plan | `min(0.03T, 8K)` | 8,000 | 8,000 | 8,000 |
| headroom | `T − TARGET` | 106,500 | 166,500 | 226,500 |

### 5.2 实测修正后的参数表（**以此为准**）

固定项取绝对值（§0.1），缩放项仍按 T 的比例。`SYS_ACTUAL = 17,590`（coding 实测）。

| 参数 | 定值方式 | 保守 (0.40) | **平衡 (0.60)** | **激进 (0.80)** |
|---|---|---|---|---|
| `context_window` | `floor(1M × P)` | 400,000 | **600,000** | **800,000** |
| `system_prompt` slot | **绝对值 24,000** | 24,000 | 24,000 | 24,000 |
| `tool_schema` slot | **绝对值 8,000** | 8,000 | 8,000 | 8,000 |
| `conversation` slot | `CW − 32,000` | 368,000 | **568,000** | 768,000 |
| BUFFER | 固定 | 5,000 | 5,000 | 5,000 |
| **T 触发阈值** | `CONV − BUFFER` | **363,000** | **563,000** | **763,000** |
| TARGET | `0.70T` | 254,100 | 394,100 | 534,100 |
| REINJECT | **实测 0**（未启用） | 0 | **0** | **0** |
| RESERVED | **实测 512** | 512 | **512** | **512** |
| `SYS_ACTUAL` | 实测（coding） | 17,590 | **17,590** | 17,590 |
| KEEP_BUDGET | 见 §4 | 235,998 | 375,998 | 515,998 |
| KEEP_CAP | `min(0.45T, budget)` | 163,350 | **253,350** | **343,350** |
| MIN_KEEP | `min(0.12T, KEEP_CAP)` | 43,560 | **67,560** | **91,560** |
| TOOL_RESULT_CAP | **实测 20,000** | 20,000 | 20,000 | 20,000 |
| headroom | `T − TARGET` | 108,900 | 168,900 | 228,900 |

> **与 §5.1（原始 spec 口径）的差异**：T 从 555,000 → **563,000**（+1.4%），
> 但**构成完全不同**：system 从 16,000 → **24,000**、tool 从 24,000 → **8,000**、
> RESERVED 从 32,000 → **512**、REINJECT 从 60,000 → **0**。
> **总数碰巧接近，单项全错。**
>
> **office 模式**：`SYS_ACTUAL = 2,236` → `KEEP_BUDGET = 391,352`，其余同表。
> 可视对照见 `COMPACT_THRESHOLD_MAP.html` 的「口径 / system_prompt 模式」切换。

> 1M 模型的两个目标值：**平衡 T=555,000**（CW=600,000）、**激进 T=755,000**（CW=800,000）。
> 保留窗口 KEEP_CAP 分别约 **250K / 340K** —— 这才是"真的在用 1M 窗口"。

---

## 6. 需求条目

| ID | 需求 | 验收方式 |
|---|---|---|
| R1 | 新增 `model_id → model_max_context` 映射表与解析器，未命中回落 128,000 + warning | 未知模型不崩溃，日志有 warning |
| R2 | 档位 = 窗口比例：`conservative/balanced/aggressive → 0.40/0.60/0.80`，实际 CW 由解析器算出 | `run_local.py` 无裸数字 |
| R3 | 所有预算由 `ContextWindowBudget` 按 §4 派生；SDK 内不再有 token 绝对值常量（**绝对上限常量除外**，它们是设计的一部分） | grep 校验 |
| R4 | 固定尺寸预算（sys/tool/reinject/reserved/tool_result）一律用 `min(比例, 绝对上限)` 双尺子 | 1M 下 REINJECT ≤ 60K |
| R5 | `compact_if_needed` 计算 `target_tokens` 时**显式预留 RESERVED 与 REINJECT**，消除压缩后溢出 | 压力测试无 `context_overflow` 停机 |
| R6 | `BUFFER` 真正生效：触发线 = `CONV − BUFFER` | 单测断言触发点 |
| R7 | `recall_ratio` 默认 0.00，配额全部划给 conversation | 1M 下无未分配余量 |
| R8 | `Memory.init_from_restore` 使用 `self._compact_threshold` 而非模块级默认 | 配置 555K 时恢复预算 = 555K |
| R9 | `loop.py` 调用 `build_skill_summaries` / `build_agent_summaries` 补传 `context_window` | 两处摘要预算随 CW 变化 |
| R10 | 档位门槛 I6：不满足时自动降级 + warning | 200K 模型选激进 → 降级为平衡 |
| R11 | 新增 `memory/tests/test_compact_if_needed.py`，覆盖 6 场景（§9） | 测试全绿 |
| R12 | 向后兼容：SDK 默认档（CW=128,000 / R_conv=0.50）仍可用 | 既有测试通过 |
| **R13** | 固定尺寸槽位（`system_prompt` / `tool_schema` / `RESERVED` / `REINJECT` / `TOOL_RESULT_CAP`）一律用**实测绝对值**常量，禁用比例推导 | 见 §0.1 / §5.2 |
| **R14** | `system_prompt` 配额改绝对值 24,000，并按 mode（coding / office）分别校验 | G1 |
| **R15** | `static_context` 截断判据改用注入的 token estimator（当前用 `chars/4`，低估 1.85x） | G2 |
| **R16** | 为工作区 prompt 片段（`PANDAPAL.md` 等）增加独立 `max_tokens`，超限截断该片段 | G3 |
| **R17** | `recall_ratio` 默认 0.0，回收 10,000 配额 | G4 |
| **R18** | 所有"判据 + 裁剪"统一走同一个 `token_estimator`（消灭 5 套口径） | G11 |
| **R19** | `TOOL_RESULT_CAP` 裁剪改为按 estimator 复估收敛（修中文"越截越大"） | G10 / P12 |
| **R20** | MCP `inputSchema` 加体积校验 + 默认强制 DEFERRED + 单 server 工具数上限 | G6 |
| **R21** | 修 `ToolBudget.enforce` 的下限语义（总长 ≤ 15 时预算整体失效） | G7 / P11 |
| **R22** | `DEFAULT_POST_COMPACT_TOKEN_BUDGET = 50,000` 下调到阈值的 10~15% | G15 |
| **R23** | `engine/loop.py:199,203` 补传 `context_window`（skill/agent 摘要预算与配置脱钩） | G14 |
| **R24** | 测量台接入 CI：断言"各项实占 ≤ 对应配额"（当前 `system_prompt[coding]` 就已失败） | G12 |

---

## 7. 跨模型窗口的适用性

| 模型标称 | 保守 0.40 | 平衡 0.60 | 激进 0.80 | 备注 |
|---|---|---|---|---|
| 128K | 51,200 | 76,800 | 102,400 | 激进需 `max_tokens ≤ 12,800`（I1） |
| 200K | 80,000 | 120,000 | 160,000 | 激进需 `max_tokens ≤ 20,000` |
| 400K | 160,000 | 240,000 | 320,000 | 全部可用 |
| **1M** | 400,000 | **600,000** | **800,000** | 全部可用 |

> ⚠️ 上表是**原始 spec 口径**（system 16,000 / tool 24,000）。
> 实测修正后固定槽位改为绝对值（system 24,000 / tool 8,000），
> 各档的 T 变为 **363,000 / 563,000 / 763,000**（见 §5.2）。

档位门槛（I6）的通用形式：`max_tokens ≤ (0.9 − P) × model_max_context`。

- P=0.60 → `max_tokens ≤ 0.30 × model_max_context`（1M 下 ≤300K，永不触发）
- P=0.80 → `max_tokens ≤ 0.10 × model_max_context`（1M 下 ≤100K，常规 32K 输出安全）

---

## 8. 文件级变更清单

### 新增
| 文件 | 内容 | 状态 |
|---|---|---|
| `scripts/measure_context_budget.py` | **实测测量台**（真 tokenizer + 真对象实例），治理依据 | ✅ 已完成 |
| `pandaren/memory/COMPACT_BUDGET_AUDIT.md` | **实测体检报告 + 治理清单**（G1~G15） | ✅ 已完成 |
| `pandaren/memory/COMPACT_THRESHOLD_MAP.html` | 可视化（含「实测修正 / 原始 spec」口径切换） | ✅ 已完成 |
| `pandapal/config/llm/model_context_windows.toml` | `model_id → model_max_context`（含 1M 条目） | ⬜ 待做 |
| `pandapal/config/llm/context_window_resolver.py` | `resolve_model_window(model_id)`、`resolve_profile(...)`（含 I6 降级） | ⬜ 待做 |
| `pandaren/memory/tests/test_compact_if_needed.py` | R11 测试 | ⬜ 待做 |

### 修改
| 文件 | 改动 |
|---|---|
| `pandaren/memory/constants.py` | 比例常量 + 绝对上限常量；`DEFAULT_COMPACT_BUFFER_TOKENS` 接入；清理死常量 |
| `pandaren/behavior/context_window_budget.py` | 新增派生 property：`sys_cap_tokens` / `tool_schema_cap_tokens` / `t_tokens` / `target_tokens` / `reinject_tokens` / `reserved_tokens` / `keep_cap_tokens` / `min_keep_tokens` / `tool_result_cap_tokens` |
| `pandaren/builder.py:1058-1062` | 注入 `compact_threshold` 的同时派生 `WindowedKeepPolicy` 与 `post_compact_token_budget` |
| `pandaren/memory/memory.py:696-704` | `target_tokens` 减去 `RESERVED` 与 `REINJECT`（R5） |
| `pandaren/memory/memory.py:477` | 改用 `self._compact_threshold`（R8） |
| `pandaren/memory/__init__.py` | 同步 re-export |
| `pandaren/engine/loop.py:199,203` | 补传 `context_window`（R9） |
| `pandapal/local/run_local.py:520-525` | 改为档位 + 解析器（R1/R2） |

---

## 9. 验收标准

`test_compact_if_needed.py` 覆盖：

1. 未超阈值 → 返回 `None`，不写 boundary；
2. MicroCompact 单独解决 → 不写 boundary、不触发 `on_compact_callback`；
3. 正常压缩 → `kept + dropped` 为完整划分、`final_total < T`；
4. **回注后不溢出**（R5 回归用例，核心）；
5. 反扩保护：`kept >= original` → 放弃压缩并返回 `current_tokens`；
6. system + attachments 自超阈值 → 返回 `current_tokens` + warning。

另需：

- 1M 档位下跑通，断言 `KEEP_CAP ≈ 250K / 340K`（R4）；
- 400K / 200K / 128K 三档 I1/I6 校验通过；
- 离线回放 raw_log，确认无 `TerminalReason.CONTEXT_OVERFLOW`。

---

## 10. 风险与明确不做

### 风险（需知会）

1. **成本**：CW=600K/800K 意味着每步请求可能携带 50 万级 token，Agent 200 步就是天量输入。必须依赖
   prompt cache（`llm/client.py` 的 cache 策略 + `model_prices.toml` 的 `cache_read_price_per_1k`）；
   无 cache 命中的厂商（见 `run_local.py:476-478` 的 `include_usage` 说明）成本会失控。
2. **延迟**：600K 输入的首 token 延迟显著，交互式体验下降。
3. **有效窗口**：长上下文普遍 lost-in-the-middle，标称 800K 的有效信息容量低于线性预期。
4. **档位应可配**：建议按 session/任务类型选择档位，长文档任务用激进、日常问答用保守。

### 明确不做

- 不引入 `model_id → CW` 映射进 SDK（`context_window_budget.py:11-14` 的边界保持）；
- 不恢复已废弃的 recall / 跨 session 召回；
- 不改变 `ContextWindowBudget` 的 `frozen` 语义与 `Memory._FROZEN_ATTRS` 冻结机制；
- 不新增异常类：溢出仍由 `compact_if_needed` 返回值 + `TerminalReason.CONTEXT_OVERFLOW` 驱动。

---

# 附录 A · 字段人话辞典

> 比喻：把它当成**一张办公桌**。桌子总大小 = 模型窗口（厂商定死）；报警 = 触发压缩；
> 清仓 = 把旧资料收进档案柜只留最近的；便利贴 = 清仓后重新贴回的关键信息。

| 字段 | 一句话含义 | 谁在用 | 1M 平衡 | 1M 激进 |
|---|---|---|---|---|
| `model_max_context` | 模型一次最多能"看"多少 token，厂商定死 | 模型配置 | 1,000,000 | 1,000,000 |
| `max_tokens` | 这次要让模型**写**多少字（输出） | `llm_settings` | 32,000 | 32,000 |
| `context_window` (CW) | 主动划给自己的**输入**额度 | 应用层档位 | 600,000 | 800,000 |
| `system_prompt` slot | 人设/系统提示词最多占多少，超了截断 | `engine/loop.py` | 16,000 | 16,000 |
| `tool_schema` slot | 所有工具的 JSON 说明书占多少 | `ToolBudget` | 24,000 | 24,000 |
| `conversation` slot | 留给聊天历史 + 工具结果的额度 | — | 560,000 | 760,000 |
| `BUFFER` | 不想贴着线才动手，提前这么多触发 | `memory` | 5,000 | 5,000 |
| **`T` / `compact_threshold`** | **★ 压缩触发线：估算超过它就清仓** | `compact_if_needed` | **555,000** | **755,000** |
| `TARGET` | 清仓后希望降到多少（0.70 × T） | 切分策略 | 388,500 | 528,500 |
| `RESERVED` | 清仓时要调 LLM 写摘要，预留它的输出位置 | `DropSummarizer` | 32,000 | 32,000 |
| `REINJECT` | 清仓后重贴的"最近文件/技能/plan"预算 | `PostCompactReinjector` | 60,000 | 60,000 |
| `KEEP_BUDGET` | TARGET 扣掉 system、摘要、便利贴后**真正**留给对话的 | `WindowedKeepPolicy` | 290,500 | 428,500 |
| `KEEP_CAP` | 清仓后**最多**留多少对话 | 同上 | 249,750 | 339,750 |
| `MIN_KEEP` | **至少**留这么多，保证对话不断片 | 同上 | 66,600 | 90,600 |
| `SYS_ACTUAL` | 真实 system 提示词多大（上限 16,000） | 实测 | ~6,000 | ~8,000 |
| `OLD_ATT` | 上一轮清仓贴的便利贴还挂着，先扣掉 | — | 0 | 0 |
| `TOOL_RESULT_CAP` | **单条**工具结果上限，超了当场截断 | `MicroCompactor` | 30,000 | 30,000 |
| `headroom` | 触发线到清仓目标的空档，防止刚清完又触发 | — | 166,500 | 226,500 |

大小关系速记：

```
CW (600,000)                ← 打算最多用多少
 └ conversation (560,000)   ← 其中给对话的
    └ T (555,000)           ← 超过就清仓（少一个 BUFFER）
       └ TARGET (388,500)   ← 清仓后想降到
          └ KEEP_CAP (249,750) ← 清仓后最多留
             └ MIN_KEEP (66,600) ← 至少要留
```

---

# 附录 B · token 换算表

## B.1 换算系数（经验值，随分词器浮动）

| 语言 | 换算 |
|---|---|
| 英文 | 1 token ≈ **4 字符** ≈ **0.75 个单词** |
| 中文 | 1 token ≈ **1 个汉字**（保守）；中文优化分词器（Qwen/GLM 系）≈ **1.4 个汉字** |
| 代码 | 1 token ≈ **3.5 字符** ≈ **0.09 行**（按每行 40 字符） |

## B.2 通用对照

| token | ≈英文单词 | ≈英文字符 | ≈汉字（保守） | ≈汉字（中文优化） | ≈代码行数 |
|---|---|---|---|---|---|
| 1,000 | 750 | 4,000 | 1,000 | 1,400 | 87 |
| 5,000 | 3,750 | 20,000 | 5,000 | 7,000 | 440 |
| 10,000 | 7,500 | 40,000 | 1 万 | 1.4 万 | 870 |
| 16,000 | 1.2 万 | 6.4 万 | 1.6 万 | 2.2 万 | 1,400 |
| 24,000 | 1.8 万 | 9.6 万 | 2.4 万 | 3.4 万 | 2,100 |
| 30,000 | 2.25 万 | 12 万 | 3 万 | 4.2 万 | 2,600 |
| 32,000 | 2.4 万 | 12.8 万 | 3.2 万 | 4.5 万 | 2,800 |
| 60,000 | 4.5 万 | 24 万 | 6 万 | 8.4 万 | 5,300 |
| 66,600 | 5 万 | 26.6 万 | 6.7 万 | 9.3 万 | 5,800 |
| 100,000 | 7.5 万 | 40 万 | 10 万 | 14 万 | 8,700 |
| 250,000 | 18.7 万 | 100 万 | 25 万 | 35 万 | 2.2 万 |
| 340,000 | 25.5 万 | 136 万 | 34 万 | 47.6 万 | 3 万 |
| 428,500 | 32.1 万 | 171 万 | 42.9 万 | 60 万 | 3.8 万 |
| 555,000 | 41.6 万 | 222 万 | 55.5 万 | 78 万 | 4.9 万 |
| 600,000 | 45 万 | 240 万 | 60 万 | 84 万 | 5.3 万 |
| 800,000 | 60 万 | 320 万 | 80 万 | 112 万 | 7 万 |

## B.3 实物参照

| 参照物 | ≈token |
|---|---|
| 一条微信消息（20 字） | 20 |
| 一页 A4 中文（约 800 字） | 800 |
| 一页 A4 英文（约 500 词） | 670 |
| 一篇 3,000 字的中文文章 | 3,000 |
| 一份 1 万字的中文技术文档 | 1 万 |
| 一个 1,000 行的 Python 文件 | 8,700 |
| 《活着》全文（约 12 万字） | 12 万 |
| 《三体》第一部（约 20 万字） | 20 万 |
| 《三体》三部曲（约 88 万字） | 88 万 |
| 中型 Python 仓库（5 万行代码） | 57 万 |

## B.4 本方案数值的人话版

**1M · 平衡档（T = 555,000）**

| 字段 | token | 人话 |
|---|---|---|
| `T` 触发线 | 555,000 | 上下文堆到 **≈55 万字**（约 4.6 本《活着》）就清仓 |
| `KEEP_CAP` 保留上限 | 249,750 | 清仓后最多留 **≈25 万字**（2 本《活着》/ 1.5 万行代码） |
| `MIN_KEEP` 保留下限 | 66,600 | 至少留 **≈6.7 万字** |
| `TOOL_RESULT_CAP` | 30,000 | 单次工具返回最多 **≈3 万字 / 2,600 行代码** |
| `REINJECT` | 60,000 | 清仓后重贴的便利贴最多 **≈6 万字** |
| `RESERVED` | 32,000 | 写摘要最多 **≈3.2 万字** |
| `max_tokens` | 32,000 | 模型每次回答最多 **≈3.2 万字** |

**1M · 激进档（T = 755,000）**

| 字段 | token | 人话 |
|---|---|---|
| `T` 触发线 | 755,000 | 堆到 **≈75 万字**（接近《三体》三部曲）才清仓 |
| `KEEP_CAP` 保留上限 | 339,750 | 清仓后最多留 **≈34 万字**（2.8 本《活着》/ 2 万行代码） |
| `MIN_KEEP` 保留下限 | 90,600 | 至少留 **≈9.1 万字** |
| `KEEP_BUDGET` | 428,500 | 留给对话的总额度 **≈43 万字** |

### 一句话记忆

- `T` = **什么时候清仓**（1M 平衡 55 万字 / 激进 75 万字）
- `KEEP_CAP` = **清仓后最多留多少**（25 万字 / 34 万字）
- `TOOL_RESULT_CAP` = **单个工具结果最大 3 万字（2,600 行代码）**
- 其余字段都是围绕这三个的加减法
