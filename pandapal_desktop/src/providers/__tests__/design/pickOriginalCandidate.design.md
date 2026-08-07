# 测试设计：`pickOriginalCandidate`

> 被测目标：`src/providers/BackendProvider.tsx:97-103` 导出函数
> `pickOriginalCandidate(candidates: ReadonlyArray<string | null | undefined>, suggested: string): string | null`
>
> 用途：edit_file 场景下，从多个「修改前」候选源（suggestion 基线 / TOOL_START 读盘兜底 / loadAndOpenFile 打开缓存）中挑选真正的「修改前」内容，供 diff 使用。候选源与后端写盘存在竞态，可能全部拿到「修改后」内容。
>
> 技术栈：TS + vitest（**当前 pandapal_desktop 无任何测试基础设施**，落地需新增 devDependency，见 §9）。
> 测试层级：全部为 **unit**（纯函数，零 mock）。

---

## 0. 关键决议记录（先读）

| # | 决议 | 依据 | 状态 |
|---|------|------|------|
| **R1** | **「全部有效候选 == suggested」时返回 `valid[0]`（= suggested），而非 null** | 需求概述句写「全部相同或全为空则返回 null」，但需求自身的等价类枚举明确写「全部与 suggested 相同（**返回第一个有效候选**）」，且实现为 `valid.find(c => c!==suggested) ?? valid[0] ?? null` → 返回 `valid[0]`。调用方（BackendProvider.tsx:382）据此 `changed = suggested !== orig` 为 false，走 line 391-395「race likely」留痕分支跳过 diff——**不是静默消失**。若真改成返回 null，调用方 line 370 分支会吞掉 race 留痕日志（诊断信息丢失）。 | **按枚举+实现裁决；概述句视为措辞不精确。⚠️ 需用户确认** |
| R2 | `null` 与 `undefined` 在 filter 谓词 `typeof c === "string"` 下行为完全一致，归为**同一等价类**（无效候选），各取代表值即可 | 实现语义 | 确定 |
| R3 | 空串 `""`（`length > 0` 过滤）与 null/undefined 同为「无效候选」；但**仅含空白字符的串（如 `" "`）是有效候选**（契约未定义过滤空白）。调用方 `orig.length === 0` 异常守卫（line 377）对本函数是**死代码**（函数永不返回空串），仅为防御 | 实现语义 | 确定 |
| R4 | 相等判定为严格 `===`：大小写不同、CRLF vs LF 均视为**不同串** → 会命中「与 suggested 不同」分支被选中。行尾归一化是调用方职责（line 388-389 `normalizeEol`），函数不做 | 实现语义 + 调用方职责注释 | 确定 |

---

## 1. 白盒语义推导

```ts
// BackendProvider.tsx:97-103
const valid = candidates.filter((c): c is string => typeof c === "string" && c.length > 0);
return valid.find((c) => c !== suggested) ?? valid[0] ?? null;
```

单表达式实现，无可执行语句分支，但语义上有 5 个决策点（本设计的覆盖准则）：

| 决策点 | 条件 | 结果 |
|--------|------|------|
| **D1** filter 谓词 = true | `typeof c === "string" && c.length > 0` | 进入有效候选集 |
| **D2** filter 谓词 = false | 否则（null / undefined / `""`） | 被过滤，透明 |
| **D3** find 命中 | 存在 `valid[i] !== suggested` | 返回**第一个**（数组序）≠ suggested 的有效候选 |
| **D4** find 未命中但 valid 非空 | 全部有效候选 `=== suggested` | 返回 `valid[0]`（= suggested） |
| **D5** valid 为空 | 无任何有效候选 | 返回 `null` |

覆盖目标：**D1–D5 全部真假走到**（等价于分支覆盖 100%）。

---

## 2. 不变式（inv）

| # | 不变式 | 属性测试入口 |
|---|--------|:--:|
| inv-1 | **确定性**：同输入 → 同输出（无 I/O、无随机、无时间依赖） | [property] |
| inv-2 | **值域契约**：`r === null ⟺ 有效候选集为空`；`r ≠ null ⟹ r 是候选集内的非空串` | [property] |
| inv-3 | **首选正确性**：存在 ≠ suggested 的有效候选 ⟹ `r` = **数组序第一个** ≠ suggested 的有效候选 | [property] |
| inv-4 | **兜底链**：无 ≠ suggested 的有效候选 且 有效候选非空 ⟹ `r = valid[0]`（= suggested）；有效候选为空 ⟹ `r = null` | [property] |
| inv-5 | **无副作用**：不修改入参数组（filter 新建数组）；无外部状态读写 | [property] |

---

## 3. 风险清单（严重度 × 可能性 → 优先级）

| # | 风险 | 影响 | S | L | 级 |
|---|------|------|---|---|---|
| Risk-1 | **空/全无效候选处理错误**：全空却返回非 null（或返回空串），调用方拿到「空 original + 非空 suggested」→ 全文件绿色误报（需求明确此危害，line 376-381 注释） | UI 误报、用户误 Accept | 高 | 中 | **P1** |
| Risk-2 | **竞态下混选错误**：候选集同时含「修改前」与「修改后」（suggested）时选错（选了 == suggested 的）→ original 实为修改后，diff 反转/消失 | diff 静默消失 | 高 | 高 | **P1** |
| Risk-3 | **全部候选 == suggested（竞态全败）**：语义被改坏（如 find 反向找、把相等误判为不同）→ 显示本不该显示的 diff 或误报变更 | 误报/漏报 | 高 | 中 | **P1** |
| Risk-4 | **连续编辑基线错误**：顺序语义错误（返回「最后一个」而非「第一个」≠ suggested 的候选）→ 低优先级数据源（打开缓存）被当基线，早期修改被吞进 original → diff 消失 | 连续编辑时 diff 消失 | 中 | 中 | **P2** |
| Risk-5 | **边界退化**：仅空白串候选、超长串（MB 级）、大小写/行尾差异、`suggested=""` 退化输入 | 轻微/边界体验 | 低 | 低 | **P3** |

**非功能维度（性能/并发）说明**：该维度**不适用**——候选数恒 ≤3（调用方固定传 3 个），filter+find 线性且无可变状态；并发竞态发生在调用方的异步读盘环节，**不在本函数职责内**（本函数是纯同步选择器，无并发面）。故不设计并发/性能用例。

---

## 4. Oracle 策略

- **Golden value（可人工推导）**：本函数输出 = 输入候选集中的某个元素（恒等选择，非计算变换），期望值可从输入直接目视推导，**不构成自指 oracle**。
- **Property（属性测试）**：inv-1~inv-5 对「一整类随机输入」成立，用 fast-check 生成随机候选数组断言（用例13，标 `[property]`）。
- 不涉及参考实现/蜕变关系（无第二实现可比对，且输出可预知）。

---

## 5. Mock / Fake 策略

- **函数本身：零 mock**（纯函数，无协作对象）。
- **⚠️ 模块级注意**：函数 export 自 `BackendProvider.tsx`，import 该模块会连带执行 React / zustand stores / `@tauri-apps/api`(event, core) / `@tauri-apps/plugin-fs` 的顶层 import。Tauri JS 包装类在 import 时不触底（调用时才访问 `window.__TAURI_INTERNALS__`），vitest 下通常可导入；若出现 import 期报错，**推荐方案**：将纯函数抽取为独立模块 `src/providers/pickOriginalCandidate.ts`（无任何外部依赖），BackendProvider 改 re-export——测试直接 import 纯模块，零 mock、零环境耦合。此为设计级建议，不影响下述用例的断言本身。

---

## 6. 等价类划分

**输入空间二维**：`candidates`（元素类型 × 数组长度）+ `suggested`。

候选元素类型（6 类）：
| 类 | 定义 | 代表值 |
|----|------|--------|
| E1 | 有效串，`!== suggested`（修改前） | `'const a = 0;\n'` |
| E2 | 有效串，`=== suggested`（修改后） | `'const a = 1;\n'` |
| E3 | 空串（被过滤） | `''` |
| E4 | `null`（被过滤） | `null` |
| E5 | `undefined`（被过滤） | `undefined` |
| E6 | 仅空白串（有效，R3） | `'   '` |

数组长度：0 / 1 / 2 / 3（真实调用恒为 3，但函数接受任意长度，测 0 与 3 两个端点 + 中间）。
`suggested`：正常非空串为主；`''` 为退化边界（P3）。

---

## 7. 用例 × 风险/决策点覆盖矩阵

| 用例 | inv-1 | inv-2 | inv-3 | inv-4 | inv-5 | Risk-1 | Risk-2 | Risk-3 | Risk-4 | Risk-5 | D1 | D2 | D3 | D4 | D5 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| 1 空数组 | | ✅ | | ✅ | ✅ | ✅ | | | | | | | | | ✅ |
| 2 全无效候选 | | ✅ | | ✅ | ✅ | ✅ | | | | | | ✅ | | | ✅ |
| 3 单个候选≠suggested | | ✅ | ✅ | | ✅ | | ✅ | | | | ✅ | | ✅ | | |
| 4 首==suggested后续≠ | | ✅ | ✅ | | ✅ | | ✅ | | | | ✅ | | ✅ | | |
| 5 多个≠suggested | | ✅ | ✅ | | ✅ | | | | ✅ | | ✅ | | ✅ | | |
| 6 全部==suggested | | ✅ | | ✅ | ✅ | | ✅ | ✅ | | | ✅ | | | ✅ | |
| 7 无效元素前置 | | ✅ | ✅ | | ✅ | ✅ | ✅ | | | | ✅ | ✅ | ✅ | | |
| 8 混合大杂烩 | ✅ | ✅ | ✅ | | ✅ | | ✅ | | ✅ | | ✅ | ✅ | ✅ | | |
| 9 仅空白串 | | ✅ | ✅ | ✅ | ✅ | | | | | ✅ | ✅ | | ✅ | ✅ | |
| 10 超长串 | ✅ | ✅ | ✅ | | ✅ | | | | | ✅ | ✅ | | ✅ | | |
| 11 大小写/行尾差异 | | ✅ | ✅ | | ✅ | | | | | ✅ | ✅ | | ✅ | | |
| 12 suggested="" 退化 | ✅ | ✅ | ✅ | ✅ | ✅ | | | | | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| 13 [property] 随机生成 | ✅ | ✅ | ✅ | ✅ | ✅ | | | | | | 全 | 全 | 全 | 全 | 全 |

D1–D5 全部真假走到；三条 P1 风险每条至少 2 个用例钉死。

---

## 8. 用例详情

> 所有用例：纯函数、无副作用（仅验证返回值）；Given 除注明外无前置。
> 覆盖准则列标注其服务的决策点（D1–D5）。

---

#### 用例1：空数组返回 null

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-1 空候选 [P1] + inv-2 值域 + inv-4 兜底 |
| 测试层级 | unit |
| 覆盖准则 | D5（valid 为空 → null） |
| Oracle | golden value（可目视推导） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：数组长度 = 0 → 代表值 = `[]`

**When**：
- `pickOriginalCandidate([], 'const a = 1;\n')`

**Then**：
- 返回值 = `null`
- 副作用：无副作用（输入数组未被修改，下同）

---

#### 用例2：全无效候选（null/undefined/空串混合）返回 null

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-1 空 original 误报 [P1] + inv-2 + inv-4 |
| 测试层级 | unit |
| 覆盖准则 | D2（filter 全 false）+ D5 |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：元素 ∈ {E3 空串, E4 null, E5 undefined}，无任何有效候选（R2：null 与 undefined 同类）→ 代表值 = `[null, undefined, '']`

**When**：
- `pickOriginalCandidate([null, undefined, ''], 'const a = 1;\n')`

**Then**：
- 返回值 = `null`（绝不返回 `''` / `undefined`——否则调用方「空 original + 非空 suggested」触发全文件绿色误报）
- 副作用：无

---

#### 用例3：单个有效候选且 ≠ suggested，返回该候选

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-2 竞态下正确选「修改前」[P1] + inv-2 + inv-3 |
| 测试层级 | unit |
| 覆盖准则 | D1 + D3（find 命中） |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：长度 = 1，元素 ∈ E1（≠ suggested）→ 代表值 = `['const a = 0;\n']`，suggested = `'const a = 1;\n'`

**When**：
- `pickOriginalCandidate(['const a = 0;\n'], 'const a = 1;\n')`

**Then**：
- 返回值 = `'const a = 0;\n'`（修改前内容被正确选出 → 调用方 `changed=true`，diff 正常展示）

---

#### 用例4：首候选 == suggested、后续存在 ≠，返回第一个 ≠ 的候选

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-2 混选正确性 [P1] + inv-3 |
| 测试层级 | unit |
| 覆盖准则 | D1 + D3（find 跳过相等元素） |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：候选含 E2（== suggested）在前、E1（≠ suggested）在后 → 代表值 = `['const a = 1;\n', 'const a = 0;\n']`，suggested = `'const a = 1;\n'`

**When**：
- `pickOriginalCandidate(['const a = 1;\n', 'const a = 0;\n'], 'const a = 1;\n')`

**Then**：
- 返回值 = `'const a = 0;\n'`（**不是**第一个元素——find 必须跳过 == suggested 的候选；若实现误取 `valid[0]` 则 diff 消失，本用例即回归防线）

---

#### 用例5：多个 ≠ suggested 的候选，返回数组序第一个

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-4 连续编辑基线 [P2] + inv-3 顺序语义 |
| 测试层级 | unit |
| 覆盖准则 | D1 + D3（find 取首个命中） |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：有效候选 ≥2 且均 ≠ suggested → 代表值 = `['const a = 0;\n', 'const b = 2;\n', 'const a = 1;\n']`，suggested = `'const a = 1;\n'`

**When**：
- `pickOriginalCandidate(['const a = 0;\n', 'const b = 2;\n', 'const a = 1;\n'], 'const a = 1;\n')`

**Then**：
- 返回值 = `'const a = 0;\n'`（**数组序第一个** ≠ suggested 的候选；对应真实调用：候选顺序 = [suggestion 基线, 读盘兜底, 打开缓存]，取第一个即最高优先级数据源——若实现返回「最后一个」，早期修改会被低优先级缓存吞掉 → 连续编辑 diff 消失）

---

#### 用例6：全部有效候选 == suggested，返回第一个有效候选（= suggested）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-3 竞态全败语义 [P1] + inv-4 兜底 + **决议 R1** |
| 测试层级 | unit |
| 覆盖准则 | D4（find 未命中 + valid 非空） |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：有效候选 ≥1 且全部 === suggested → 代表值 = `['const a = 1;\n', 'const a = 1;\n']`，suggested = `'const a = 1;\n'`

**When**：
- `pickOriginalCandidate(['const a = 1;\n', 'const a = 1;\n'], 'const a = 1;\n')`

**Then**：
- 返回值 = `'const a = 1;\n'`（= `valid[0]` = suggested；⚠️ **不是 null**，见决议 R1）
- 语义链路：调用方 `changed = ('const a = 1;\n' !== 'const a = 1;\n') === false` → 走 line 391-395「race likely」分支**显式留痕跳过 diff**（不静默、不误报绿色）
- 若用户确认 R1 按字面需求改为返回 `null`，则本用例需改断言为 `null` 并同步检查调用方 line 370/391 两分支合并后的日志语义——**待确认，见 §10**

---

#### 用例7：无效元素（null/""/undefined）前置，被透明过滤，返回第一个有效 ≠ 候选

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-1 + Risk-2 [P1] + inv-2 + inv-3 |
| 测试层级 | unit |
| 覆盖准则 | D2（无效被滤）+ D3 |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：无效元素（E3/E4/E5）置于数组头部、有效候选随后 → 代表值 = `[null, '', undefined, 'const a = 0;\n', 'const a = 1;\n']`，suggested = `'const a = 1;\n'`

**When**：
- `pickOriginalCandidate([null, '', undefined, 'const a = 0;\n', 'const a = 1;\n'], 'const a = 1;\n')`

**Then**：
- 返回值 = `'const a = 0;\n'`（无效元素完全透明，不影响「第一个 ≠ suggested」的选取）

---

#### 用例8：混合大杂烩（suggested 出现多次 + 多个不同候选 + 无效元素）——对应真实竞态全形态

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-2 混选 + Risk-4 顺序 [P1/P2] + inv-1 + inv-3 |
| 测试层级 | unit |
| 覆盖准则 | D2 + D3（最坏形态下取第一个不同） |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：混合 E1/E2/E3/E4/E5 全形态 → 代表值 = `[undefined, 'const a = 1;\n', 'const a = 0;\n', '', 'const a = 1;\n', 'const c = 3;\n']`，suggested = `'const a = 1;\n'`

**When**：
- `pickOriginalCandidate([undefined, 'const a = 1;\n', 'const a = 0;\n', '', 'const a = 1;\n', 'const c = 3;\n'], 'const a = 1;\n')`

**Then**：
- 返回值 = `'const a = 0;\n'`（过滤无效后 `['const a = 1;\n','const a = 0;\n','const a = 1;\n','const c = 3;\n']`，第一个 ≠ suggested 的是 `'const a = 0;\n'`；**不是** `'const c = 3;\n'`）
- 确定性：同参数再调一次，返回值不变（inv-1）

---

#### 用例9：仅空白串候选（决议 R3）——视为有效候选

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-5 边界 [P3] + inv-2 + inv-3 + inv-4 |
| 测试层级 | unit |
| 覆盖准则 | D1（`' '` 通过 `length>0`）+ D4 分支（== suggested） |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：E6 仅空白串；两个子场景（== 与 ≠ suggested）：

**When / Then（子场景 A：空白串 ≠ suggested）**：
- `pickOriginalCandidate(['   ', 'const a = 0;\n'], 'const a = 1;\n')`
- 返回值 = `'   '`（空白串是合法有效候选，`'   ' !== 'const a = 1;\n'` → 被选中；调用方会以空白 original 展示 diff——契约如此，函数不越权过滤）

**When / Then（子场景 B：全空白串 == suggested）**：
- `pickOriginalCandidate(['   ', '   '], '   ')`
- 返回值 = `'   '`（= `valid[0]`；走 D4 兜底，同用例6 语义）

---

#### 用例10：超长串（MB 级）正确选择且确定

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-5 超长 [P3] + inv-1 确定性 + inv-3 |
| 测试层级 | unit |
| 覆盖准则 | D1 + D3 |
| Oracle | golden value（期望值 = 输入中的候选本身，恒等选择可目视推导） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：串长 ≈ 1MB、两候选仅差一个字符（一个 == suggested、一个 ≠）→ 代表值 = `[suggested, suggested.slice(0, -1) + 'X']`，`suggested = 'A'.repeat(1_000_000) + 'Z'`，另一候选 = `'A'.repeat(1_000_000) + 'Y'`

**Given**：
- 构造 `suggested = 'A'.repeat(1_000_000) + 'Z'`；`before = 'A'.repeat(1_000_000) + 'Y'`

**When**：
- `const r1 = pickOriginalCandidate([suggested, before], suggested)`

**Then**：
- 返回值 = `before`（末尾一个字符差异即判定为不同串并被选中）
- 确定性：`pickOriginalCandidate([suggested, before], suggested) === r1`（inv-1）
- 无性能断言（候选数恒 ≤3，线性扫描，无风险面）；如选用例仅验证正确性

---

#### 用例11：相等判定为严格 ===（大小写 / 行尾差异视为不同）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-5 边界 [P3] + inv-3 + **决议 R4** |
| 测试层级 | unit |
| 覆盖准则 | D1 + D3（`===` 语义） |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：与 suggested 仅大小写不同 / 仅行尾不同（CRLF vs LF），均属「不同串」→ 代表值 = `['const a = 1;\r\n', 'const a = 1;\n']`，suggested = `'const a = 1;\n'`

**When**：
- `pickOriginalCandidate(['const a = 1;\r\n', 'const a = 1;\n'], 'const a = 1;\n')`

**Then**：
- 返回值 = `'const a = 1;\r\n'`（CRLF 与 LF 是不同串 → 命中「≠ suggested」分支；行尾归一化是调用方 `normalizeEol` 职责，函数不归一化——R4）
- 同理大小写：`pickOriginalCandidate(['const A = 1;\n'], 'const a = 1;\n')` 返回 `'const A = 1;\n'`

---

#### 用例12：退化输入 `suggested = ''`（调用方理论不会传入，函数不得崩溃）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | Risk-5 退化 [P3] + inv-1/2/3/4 全量 |
| 测试层级 | unit |
| 覆盖准则 | D1–D5 全覆盖（空 suggested 下各分支仍成立） |
| Oracle | golden value |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：`suggested = ''` 时，候选含非空串 / 候选全空 两类：

**When / Then（子场景 A：候选含非空串）**：
- `pickOriginalCandidate(['const a = 0;\n', ''], '')`
- 返回值 = `'const a = 0;\n'`（`''` 被过滤；`'const a = 0;\n' !== ''` → 命中）

**When / Then（子场景 B：候选全空/无效）**：
- `pickOriginalCandidate(['', null, undefined], '')`
- 返回值 = `null`（inv-4 兜底到 null，不崩溃）

---

#### 用例13：属性测试（property）——对一整类随机输入断言 inv-1~inv-5

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 确定性 + inv-2 值域 + inv-3 首选 + inv-4 兜底 + inv-5 无副作用 [property] |
| 测试层级 | unit |
| 覆盖准则 | D1–D5 全覆盖（随机驱动） |
| Oracle | property（不变式断言，非 golden value） |
| Mock | 否 — 纯函数零 mock（需新增 `fast-check` devDependency，可选） |

**等价类划分**：随机生成 `candidates`（长度 0–6，每元素从 `{null, undefined, '', suggested, 字母表其他串}` 随机取）+ `suggested`（从字母表随机取）→ 迭代 ≥100 次

**Given**：
- 字母表 `ALPHA = ['const a = 0;\n', 'const a = 1;\n', 'const b = 2;\n', '']`；生成器：`suggested` 从 ALPHA 随机取一个非空串；`candidates` 每槽位随机取 `{null, undefined, '', suggested, ALPHA 随机串}`（允许与 suggested 相同或不同，允许重复）

**When**：
- `const r = pickOriginalCandidate(candidates, suggested)`；记录 `before = JSON.stringify(candidates)`；再调 `r2 = pickOriginalCandidate(candidates, suggested)`

**Then**（对每次生成断言）：
- inv-1：`r === r2`
- inv-2：`r === null ⟺ candidates 中无非空串`；`r !== null ⟹ typeof r === 'string' && r.length > 0 && candidates.includes(r)`
- inv-3：若存在 `candidates[i]` 为非空串且 `!== suggested`，则 `r` 等于**第一个**满足该条件的 `candidates[i]`
- inv-4：若无非空串 `!== suggested` 但存在非空串，则 `r === candidates` 中第一个非空串；否则 `r === null`
- inv-5：`JSON.stringify(candidates) === before`（入参未被修改）
- 反例（变化探测）：若某次生成全部候选均为空/无效且 `r !== null`，或存在不同候选却返回 `valid[0]` → 属性测试失败，即实现回归

---

## 9. 测试落地基础设施（vitest 需新增）

`pandapal_desktop/package.json` 当前**无任何测试框架**（仅 tsc/vite）。落地最小改动：

1. `pnpm add -D vitest`（ESM 项目 `"type": "module"`，vitest 原生支持；可选 `fast-check` 供用例13）
2. vitest 可直接复用 `vite.config.ts`（`@vitejs/plugin-react`），无需独立 config；`package.json` 增加 `"test": "vitest run"`
3. 测试文件建议位置：`src/providers/__tests__/pickOriginalCandidate.test.ts`（与设计文档同级 `design/` 目录）
4. 环境：本函数无 DOM 依赖，`environment: 'node'` 即可；但 import 自 `BackendProvider.tsx` 会连带 React/stores/tauri 模块——**推荐先抽取纯函数到独立模块**（见 §5），否则测试文件顶部需 `vi.mock('@tauri-apps/api/event')`、`vi.mock('@tauri-apps/api/core')`、`vi.mock('@tauri-apps/plugin-fs')` 并设 jsdom 环境
5. 用例13 若不用 fast-check，可用手写循环 + 固定 seed 伪随机（等价满足，需在测试里注明确定性来源）

---

## 10. Known-Gap 清单

| # | 期望行为 | 现状 | 差异原因 | 处置 |
|---|---------|------|---------|------|
| G1 | 需求概述句：「全部相同或全为空 → 返回 null」 | 实现：全部有效候选 == suggested 时返回 `valid[0]`（= suggested），仅全空返回 null；调用方经 `changed===false` 分支跳过 diff 并**留痕**（line 391-395） | 概述句为措辞简化；需求自身的等价类枚举与实现一致（R1） | 按枚举+实现设计（用例6）；若用户确认按字面改实现，需同步调整用例6 断言 + 确认调用方 race 留痕日志不丢失。**待用户确认** |
| G2 | （观察项，非 gap）调用方 line 377 `if (orig.length === 0)` 守卫对本函数是死代码（函数永不返回空串） | — | 调用方防御性冗余 | 不改函数；可顺带清理，不在本设计范围 |

---

## 11. 返回主 Agent 摘要

设计文档：`pandapal_desktop\src\providers\__tests__\design\pickOriginalCandidate.design.md`
- 覆盖：纯函数 pickOriginalCandidate，5 条不变式（确定性/值域/首选/兜底/无副作用）、5 个决策点 D1-D5 全覆盖、5 条风险（P1×3：空候选误报、竞态混选、全败语义；P2 顺序基线；P3 边界）
- 13 个用例：12 个 golden-value 具体用例 + 1 个 [property] 属性测试（inv-1~5 随机断言）；全部 unit、零 mock
- 关键决议 R1（需用户确认）：「全部候选 == suggested」返回 `valid[0]` 而非 null——与需求概述句措辞冲突，但与其自身等价类枚举及实现一致，且改 null 会丢失调用方 race 留痕日志
- 关键取舍：null/undefined/空串归同一无效类（R2）；空白串视为有效（R3）；严格 === 判定不归一化行尾（R4）
- 落地注：pandapal_desktop 无测试框架，需新增 vitest devDependency；建议抽取纯函数独立模块避免连带 tauri/React 依赖
