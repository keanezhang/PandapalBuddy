# Eval Runner — 架构说明

> 本文档是 eval-runner skill 的**背景/设计说明**，供人阅读与维护，**不进入 skill 注入内容**。
> 执行指令以 `SKILL.md` 为准；两者的分工见文末「SKILL.md 与 README 的分工」。

## 整体流程（数据流视角）

一次 eval 周期 = **同一批用例 ×（有/无 skill 两组）× N 次采样**，量化对比行为差异：

```
evals.json（用例 + 机械/语义断言）
   │  run_isolated.py 自动建骨架（with/without × sample-1..N）
   ▼
with_skill / without_skill × sample-1..N
   │  每组独立执行，产出 transcript.md + outputs/ + exit_code.txt + timing.json
   ▼
grade.py ──► mech_assertions（纯脚本判分）
judge.py ──► semantic_assertions（双盲裁判 LLM）
   │  合并写回 grading.json（v2 契约，两脚本互不覆盖）
   ▼
aggregate.py ──► benchmark.json（delta + bootstrap 95% CI + verdict）
   ▼
改进建议（主 agent 基于借口原话 + 失败断言提出，经用户确认后重跑）
```

## 执行者对照

| 步骤 | 执行者 | 产物 |
|------|--------|------|
| 1 准备 | 主 agent | `evals/evals.json`（run 骨架由 run_isolated.py 自动建） |
| 2 执行样本 | 隔离 Agent（`run_isolated.py` 构建） | `transcript.md` / `outputs/` / `exit_code.txt` / `timing.json` |
| 3a 机械判分 | `grade.py`（纯脚本，无 LLM） | `grading.json` 的 `mech_assertions` |
| 3b 语义判分 | 裁判 LLM（`judge.py` 单次调用） | `grading.json` 的 `semantic_assertions` |
| 4 聚合 | `aggregate.py`（纯脚本） | `benchmark.json` |
| 5 改进 | 主 agent（只提建议，不直接改 SKILL.md） | 修改建议 |

## 运行路径（唯一：脚本自动化）

一条命令跑完 Step 2/3/4/5：

```bash
python <eval-runner-dir>/scripts/run_isolated.py <target-skill-dir> --samples 3
```

内部自动串联：隔离执行（`AgentBuilder` 构建本地 Agent）→ `grade.py` → `judge.py` → `aggregate.py`。
可选 `--skip-judge` 只跑机械断言（省 LLM 费用）；`--only <case-id>` 单用例调试；`--variants with,without` 指定组别。

> 执行 Agent 是 `run_isolated.py` 用 AgentBuilder 构建的隔离 Agent（无 subagent 委派能力），
> 裁判是 `judge.py` 的单次 LLM 调用（无 Agent 循环）——**整个流程没有真正的 subagent**，
> 全部由脚本执行，主 agent 只负责准备用例、解释结果、提改进建议。

## 角色关系：主 agent / 执行 Agent / 裁判（三角隔离）

```
            主 agent（orchestrator）
         持有全部信息：用例、断言（mapping 由脚本私藏）
         只组织流程：调脚本、解释结果、提建议
         不判分、不执行样本
               │
     ┌─────────┴──────────┐
     ▼                    ▼
 执行 Agent（被试）      裁判 LLM（盲审者）
 不知道自己在被测         看到的是打乱标签的 A/B
 工具受限（4 个安全工具）  无文件读取能力（内容内联）
 with/without 互相隔离     不知道 A/B 谁是 with_skill
```

- **主 agent**：流程所有者，唯一同时持有执行与判分两侧信息的一方，但它不参与执行、不参与判定。
- **执行 Agent**：被测试对象，两组各自独立运行、互不可见。
- **裁判**：盲审者，只对匿名材料打分，输出 JSON 后由脚本按 mapping 映射回真实归属。

## 关键信息隔离（两层）

### 第一层：执行隔离——保证"唯一变量"

with/without 除 skill 注入外完全一致，行为差异才可归因：

| 维度 | with_skill | without_skill |
|------|-----------|---------------|
| 系统提示词 | 同一份 SYSTEM_PROMPT | 完全相同 |
| 工具集 | 仅 write/read/list/glob（无 bash、无删除） | 完全相同 |
| skill 注入 | SKILL.md 正文全文内联进 user prompt | 指令隔离：禁止加载/引用/探索该 skill 目录 |
| 物理路径 | `with_skill/sample-i/` | `without_skill/sample-i/` |

### 第二层：判分隔离（双盲）——防止裁判偏袒，由脚本物理保证

1. `build_blind_pair()`：`random.Random(seed)` 打乱 with/without → A/B，归属只存 `mapping.json`（脚本私藏，不进裁判输入）。
2. **内容内联**：`collect_transcript_package()` 把 transcript 全文 + outputs 文本文件（单文件截断 8000 字符）拼进裁判 prompt，裁判**无文件读取能力**——双盲从"主 agent 自觉"升级为"物理隔离"。
3. **可复现**：`temperature=0 + seed`，同一输入重复判分结果稳定。
4. **映射期不泄露**：`normalize_evidence_ref()` 把裁判的 `"A.md:12"` 解码为 `"with_skill/sample-1/12"`——发生在判分完成后，裁判永远看不到引用被还原成哪个 variant。
5. **禁止篡改**：`map_verdict_to_variant()` 只做结构补全（assertion 文本、severity、evidence_ref 前缀），score/evidence 原文原样保留；裁判漏判断言补 `score 0` + 显式说明（结构补全，非编造判定）。
6. **审计痕迹**：`judge/A.md`、`B.md`、`mapping.json`、`prompt.txt` 全部落盘，事后可复核。

## SKILL.md 与 README 的分工

| | SKILL.md | README.md（本文档） |
|---|---|---|
| 定位 | **执行指令知识包** | **背景/设计说明** |
| 读者 | 主 agent（skill 系统加载注入） | 人（维护者/使用者） |
| 是否进入注入 | 是——`pandaren/skill/loader.py` 只匹配 `SKILL.md` | 否——加载器不读 README |
| 自测风险 | **会被注入给被测 agent**（`run_isolated.py` 把 SKILL.md 全文注入 with_skill 组） | 无——不进任何被测上下文 |
| 内容准则 | 只写"怎么执行"的核心指令，越精炼越好 | 可写"为什么这样设计"的架构梳理 |

**为什么不能把架构说明塞进 SKILL.md**：
1. **稀释核心指令**：SKILL.md 全文注入，背景内容越长信噪比越低，主 agent 执行 5 步流程时越容易跑偏。
2. **token 成本**：每次加载 skill / 每个 with_skill sample 都全文注入，冗余内容按次计费。
3. **自测污染（最严重，潜在）**：eval-runner 的机制允许自测（`run_isolated.py` 的 target 指向自身 + 为它补一份 `evals/evals.json`）。一旦自测，SKILL.md 全文注入给被测 agent——"执行 Agent（被试）""不知道自己在被测"这类表述会直接点破评测机制，破坏执行隔离的盲性，**污染评测结果本身**。当前 eval-runner 尚未编写自己的 evals/，此风险是**潜在**而非现状；若未来补自测用例，SKILL.md 中的这类表述必须一并处理。

**维护约定**：新增内容先问"这是执行指令还是架构说明？"——前者进 SKILL.md，后者进本文档。
