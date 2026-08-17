# 测试设计：LeftSidebar / SidebarBody flex 吸收容器修复

> 目标函数：`SidebarBody({ mode })`（`src/components/LeftSidebar.tsx:303-391`）
> 相关私有组件：`ResizeHandle`（`:395-470`）、`SidebarDock`（`:225-289`）、`SidebarHeader`（`:49-86`）、`LeftSidebar`（`:474-576`）
> 测试栈：vitest + @testing-library/react，jsdom 环境（`vitest.config.ts`，`setupFiles: src/test/setup.ts`）

## 0. 变更概述与结论先行

**修复前**：`SidebarBody` 返回 React Fragment，其子元素（工作目录+分组上区 / ResizeHandle / 会话列表区）直接作为 `LeftSidebar` flex column 的 flex items，无统一 `flex:1 + minHeight:0` 吸收区。拖高会话区（固定 height + `flexShrink:0`）且窗口较矮时，上区被压到 0，`ModeSwitcher/MainNav` 因默认 `min-height:auto` 不可压缩，总高溢出被根容器 `overflow:hidden` 在底部裁剪 → `SidebarDock` 被顶出可视区。

**修复后**：office/coding 两分支均包进 `bodyStyle = { flex: "1 1 0%", minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }`，中部整体成为唯一吸收区，Dock 固定底部。

**设计目标（jsdom 限制下）**：锁定结构不变式（bodyStyle 三件套 + Dock/Header 不可收缩 + 内部分区 flex 契约）+ 可单元化的 ResizeHandle 高度钳制与 `preferenceStore.sessionPanelHeight` 状态流。**真实布局/溢出裁剪/Dock 可见性无法在 jsdom 断言，单列 playwright/人工验证清单。**

---

## 1. 风险清单（RISK 分级，S 严重度 × L 可能性）

| ID | 风险 | S | L | 优先级 | 锁定手段 |
|----|------|---|---|--------|---------|
| R1 | 修复未生效：SidebarBody 仍返回 Fragment / 无包装容器 | 高(布局损坏) | 高(每次渲染) | **P0** | inv-S1 |
| R2 | wrapper 缺 `minHeight:0` 或 `overflow:hidden` → 中部仍不能吸收收缩 / 内部溢出外泄 | 高 | 中 | **P0** | inv-S1 |
| R3 | SidebarDock 的 `flexShrink:0` 被误删 → Dock 可被压缩顶出 | 高 | 低 | **P0** | inv-S2 |
| R4 | 拖拽高度钳制失效 → 会话区可到 0 或无限 → 间接顶出 Dock | 中 | 中 | **P1** | inv-L1/L2 |
| R5 | 只包了 office 漏了 coding（或反之），切换模式后 bug 复现 | 高 | 中 | **P1** | inv-S1 两分支 |
| R6 | 拖拽过程中上区未转为可收缩（`flex:1 1 auto`+`minHeight:0`）→ 实时拖拽仍顶出 Dock | 中 | 中 | **P1** | inv-L5 |
| R7 | 双击复位失效（`sessionPanelHeight` 不回 null） | 低 | 中 | **P2** | inv-L4 |
| R8 | jsdom 无法验证真实 flex 布局（Dock 可见性 / 溢出裁剪 / 下降钳制的 offsetHeight 起点） | 中 | 低 | **P2** | 转 playwright/人工 |

## 2. 不变式清单

### 结构不变式（jsdom 可断言 inline style）
- **inv-S1**：SidebarBody 根节点（office 与 coding 两分支）inline style 恒为 `{ flex: "1 1 0%", minHeight: 0, display: "flex", flexDirection: "column", overflow: "hidden" }`。
- **inv-S2**：SidebarDock 容器 `flexShrink: 0`。
- **inv-S3**：SidebarHeader 容器 `flexShrink: 0`。
- **inv-S4**（office 会话区）：`sessionPanelHeight === null` → `{ flex: 1, overflowY: "auto", minHeight: 0 }`；非 null → `{ height: <px>, flexShrink: 0, overflowY: "auto", minHeight: 0 }`。
- **inv-S5**（office 上区）：非 null → `{ flex: "1 1 auto", overflowY: "auto", minHeight: 0 }`；null → `{ flex: "0 0 auto" }`。
- **inv-S6**（coding）：文件区 `{ flex: 1, overflowY: "auto", minHeight: 0 }`；会话区 `{ height: sessionPanelHeight ?? 180, overflowY: "auto", flexShrink: 0 }`。

### 行为不变式（可单元化）
- **inv-L1**：ResizeHandle 拖拽高度钳制在 `[120, 600]`。
- **inv-L2**：`preferenceStore.setSessionPanelHeight` 钳制 `[120,600]`，`null` 透传。
- **inv-L3**：mouseup 时将钳制后的高度提交给 `onCommit`（→ `setSessionPanelHeight`）。
- **inv-L4**：双击 ResizeHandle → `setSessionPanelHeight(null)`。
- **inv-L5**：拖拽 mousedown 将 target 钉死为 `flex: "0 0 auto"` + 显式 height；office 上区钉死为 `flex: "1 1 auto" + minHeight: 0 + overflowY: auto`。

---

## 3. Oracle 策略

| 被测输出 | Oracle 类型 | 依据 |
|---------|------------|------|
| inline style 字段值（`flex`/`minHeight`/`flexShrink`/`overflow`…） | **golden value** | 修复说明白纸黑字写死 `bodyStyle`；Dock/Header 的 `flexShrink:0` 在源码中写死；可独立推导，非"跑实现抄来" |
| `setSessionPanelHeight` 钳制结果 | **golden value** | 钳制公式 `Math.min(600, Math.max(120, h))` 在 store 源码写死，边界值可手算（50→120、9999→600、120→120、600→600） |
| ResizeHandle 拖拽后的 `sessionDiv.style.height` | **golden value** | 钳制公式 + 固定 delta 可手算（见 TC-8） |
| Dock 是否可见 / 溢出是否被裁剪 | —（jsdom 不可算） | 转 playwright 截图/布局断言 |

> 注意：不做"用 jsdom 断言 offsetHeight/scrollHeight"这类自指或恒为 0 的断言；所有 jsdom 断言均锁定在 **inline style（React 契约）** 与 **store 状态** 上。

---

## 4. Mock / Fake 策略

沿用项目既有 `settingsPanelCrash.test.tsx`（真实渲染 + 只 mock Tauri IPC）与 `InteractionInline.test.tsx`（mock `useBackend`）模式。

| 依赖 | 决策 | 理由 |
|------|------|------|
| `react-router-dom`（`useNavigate`/`useLocation`） | **真实** `<MemoryRouter>` 包裹 | jsdom 可用；MainNav / SessionListPanel / SessionGroupSection 需要，无需 mock |
| `react-i18next` | **真实** i18n：`import "../../i18n"` + `beforeEach await i18n.changeLanguage("zh-CN")` | 与两个既有测试一致；文本断言用 zh-CN golden 值 |
| `@tauri-apps/api/core`（`invoke`） | **mock**：`vi.mock` → `vi.fn()`（`vi.hoisted`） | authStore / workspaceStore 导入 invoke；jsdom 无 Tauri 运行时 |
| `@tauri-apps/plugin-fs` | **mock**：no-op 函数集 | fileStore 导入 `readTextFile/readDir/...`；结构测试设 `workspace.current=null`，本不触发 IO，mock 为防御 |
| `@tauri-apps/plugin-dialog` | **mock**：no-op（`open`/`save`） | workspaceStore / fileStore 导入 |
| `useBackend`（`../../providers/BackendProvider`） | **mock**：`vi.mock` 返回全量 no-op stub | 不挂真实 `BackendProvider`（其 useEffect 会 `listen`/`invoke`）；MainNav/SessionListPanel/SessionGroupsWrapper 解构的键必须齐全，避免 `undefined` 解构崩溃 |
| Zustand stores | **真实** store + `beforeEach setState` 种子 | 与 `seedStore` 模式一致；`preferenceStore` 保留真实 `setSessionPanelHeight`，TC-9 直接测它 |

**`useBackend` stub 必须包含的最小键集**（各组件解构用）：
`requestSessionList, createSession, switchSession, deleteSession, renameSession, groupMutate`
（建议按 `BackendContextValue` 接口补齐全量 no-op，防御未来解构新增键）。

**统一种子函数 `seedSidebarState()`**（每个用例 `beforeEach` 调用，隔离单例 store 的跨测试污染）：

```ts
usePreferenceStore.setState({ mode: "office", sidebarCollapsed: false, sessionPanelHeight: null, sidebarWidth: 260 });
useSessionStore.setState({ sessions: [], groups: [], currentSessionId: null, currentGroupFilter: "all", page: 1, hasMore: true, loading: false });
useWorkspaceStore.setState({ current: null, status: "idle", recent: [], last: null, error: null });
useFileStore.setState({ fileTree: [], fileTreeLoading: false, openFiles: [], activeFileId: null, fileContents: {}, _cacheOrder: [], suggestions: {} });
useAuthStore.setState({ status: "authenticated", username: "tester", token: null, userId: "u1", authMode: "local", error: null });
useConnectionStore.setState({ status: "connected", errorMessage: null });
useCommandPaletteStore.setState({ open: false });
await i18n.changeLanguage("zh-CN");
```

渲染方式（统一）：

```ts
render(<MemoryRouter><LeftSidebar /></MemoryRouter>);
```

---

## 5. 元素定位约定（无 data-testid，用语义定位 + DOM 遍历）

- **ResizeHandle**：`const handle = screen.getByTitle("拖动调整对话区高度，双击复位")`（`t("leftsidebar.sessionResizeHint")`，zh-CN 唯一）。
- **SidebarBody 根**：`const bodyRoot = handle.parentElement as HTMLElement`（两模式下 handle 的唯一父级即 bodyRoot）。
- **SidebarDock 容器**：`const dock = screen.getByRole("button", { name: "退出登录" }).parentElement as HTMLElement`（`t("common.logout")`）。
- **SidebarHeader 容器**：`const header = screen.getByText("PandaPal").parentElement as HTMLElement`。
- **office 上区**：`const upperDiv = handle.previousElementSibling as HTMLElement`。
- **会话区 div（两模式通用）**：`const sessionDiv = handle.nextElementSibling?.nextElementSibling as HTMLElement`（handle 后依次是 SectionHeader「对话列表」→ session div）。
- **coding 文件区**：`const fileDiv = handle.previousElementSibling as HTMLElement`。

> **inline style 数值断言口径（React 序列化规则）**：
> - 数值 `0` 的 style 属性（`minHeight: 0`、`flexShrink: 0`）→ React 序列化为字符串 `"0"`（不加 px）。
> - 数值非 0 的尺寸属性（`height: 300`/`180`/`5`）→ `"300px"`/`"180px"`/`"5px"`。
> - 字符串 flex 值（`"1 1 0%"`/`"0 0 auto"`/`"1 1 auto"`）→ 原样保留；数值 `flex: 1`（unitless）→ `"1"`。
> - 断言 `minHeight` 时若对序列化不确定，用 `expect(["0", "0px"]).toContain(el.style.minHeight)` 兼容。
> - 命令式赋值（`el.style.minHeight = "0"`）恒为字符串 `"0"`。

---

## 6. 覆盖矩阵（用例 × 风险/不变式）

| 用例 | inv-S1(两分支) | inv-S2 Dock | inv-S3 Header | inv-S4/S5 office | inv-S6 coding | inv-L1/L3 钳制 | inv-L2 store | inv-L5 mousedown | inv-L4 复位 | 优先级 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| TC-1 office 根容器样式 | ✅ | | | | | | | | | P0 |
| TC-2 coding 根容器样式 | ✅ | | | | | | | | | P0 |
| TC-3 Dock flexShrink:0 | | ✅ | | | | | | | | P0 |
| TC-4 固定外壳（Header/根 overflow） | | | ✅ | | | | | | | P2 |
| TC-5 office 分区（默认 null） | | | | ✅ | | | | | | P1 |
| TC-6 office 分区（fixed=300） | | | | ✅ | | | | | | P1 |
| TC-7 coding 分区（默认） | | | | | ✅ | | | | | P1 |
| TC-8 拖拽钳制 [120,600] + commit | | | | | | ✅ | | | | P0 |
| TC-9 store setSessionPanelHeight 钳制 | | | | | | | ✅ | | | P1 |
| TC-10 mousedown 样式钉死 | | | | | | | | ✅ | | P1 |
| TC-11 双击复位 | | | | | | | | | ✅ | P2 |

覆盖准则：**分支覆盖**（默认目标）。`SidebarBody` 的 office/coding 分支 + office 的 fixed/null 子分支均覆盖；`ResizeHandle` 的 onMove 钳制上/下界、onUp commit、onReset、onMouseDown 带/不带 upperRef 均覆盖。无复合判定，不需 MC/DC。

---

## 7. 用例详设

### 用例 TC-1：office 模式 SidebarBody 根节点携带完整吸收容器样式 [P0]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R1 [P0] + R2 [P0] + R5(office 分支) / inv-S1 |
| 测试层级 | component(fake)（真实 store + 真实组件，mock useBackend + Tauri） |
| 覆盖准则 | branch: `mode !== "coding"` 的 office 分支 |
| Oracle | golden value（`bodyStyle` 白纸黑字） |
| Mock | 见 §4 |

**等价类划分**：`mode` ∈ {office, coding} → 代表值 `office`（`seedSidebarState` 默认）

**Given**：`seedSidebarState()`（mode=office, sessionPanelHeight=null）；已 `await i18n.changeLanguage("zh-CN")`。

**When**：`render(<MemoryRouter><LeftSidebar /></MemoryRouter>)`；取 `bodyRoot = getByTitle("拖动调整对话区高度，双击复位").parentElement`。

**Then**：
- `bodyRoot.style.flex === "1 1 0%"`
- `bodyRoot.style.minHeight === "0"`（或用 `["0","0px"]` 兼容断言）
- `bodyRoot.style.display === "flex"`
- `bodyRoot.style.flexDirection === "column"`
- `bodyRoot.style.overflow === "hidden"`
- 副作用：无（纯渲染，仅验证 inline style）

---

### 用例 TC-2：coding 模式 SidebarBody 根节点同样携带吸收容器样式 [P0]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R1 [P0] + R2 [P0] + R5(coding 分支) / inv-S1 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: `mode === "coding"` 分支 |
| Oracle | golden value |
| Mock | 见 §4 |

**等价类划分**：`mode` = `coding`

**Given**：`seedSidebarState()` 后追加 `usePreferenceStore.setState({ mode: "coding" })`。

**When**：渲染后取 `bodyRoot = getByTitle("拖动调整对话区高度，双击复位").parentElement`。

**Then**：同 TC-1 的五项断言（证明修复没有漏掉 coding 分支）。

---

### 用例 TC-3：SidebarDock 容器 flexShrink:0，不被压缩 [P0]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R3 [P0] / inv-S2 |
| 测试层级 | component(fake) |
| 覆盖准则 | N/A（无分支，结构断言） |
| Oracle | golden value（源码 `:238` 写死 `flexShrink: 0`） |
| Mock | 见 §4 |

**等价类划分**：不适用（固定外壳）

**Given**：`seedSidebarState()`。

**When**：渲染后取 `dock = getByRole("button", { name: "退出登录" }).parentElement`。

**Then**：
- `dock.style.flexShrink === "0"`
- `dock` 是 LeftSidebar 根容器的最后一个 flex item（顺序断言：`dock.previousElementSibling` 即 bodyRoot，防 Dock 被意外移入 bodyRoot 内）

---

### 用例 TC-4：固定外壳契约——Header flexShrink:0 + 根容器 overflow:hidden [P2]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-S3 + 根容器裁剪边界（R2 的溢出外泄防线，pre-existing 回归锁） |
| 测试层级 | component(fake) |
| 覆盖准则 | N/A |
| Oracle | golden value |
| Mock | 见 §4 |

**等价类划分**：不适用

**Given**：`seedSidebarState()`。

**When**：渲染后取 `header = getByText("PandaPal").parentElement`；`root = header.parentElement`（即 LeftSidebar 根容器）。

**Then**：
- `header.style.flexShrink === "0"`
- `root.style.overflow === "hidden"`
- `root.style.display === "flex"` 且 `root.style.flexDirection === "column"`（外壳是 flex column 的前提，确认断言定位正确）

---

### 用例 TC-5：office 默认（sessionPanelHeight=null）分区 flex 契约 [P1]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-S4(null) + inv-S5(null) |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: office + `fixed=false` |
| Oracle | golden value |
| Mock | 见 §4 |

**等价类划分**：`sessionPanelHeight` ∈ {null, 120..600} → 代表值 `null`

**Given**：`seedSidebarState()`（sessionPanelHeight=null）。

**When**：渲染后取 `handle`、`upperDiv = handle.previousElementSibling`、`sessionDiv = handle.nextElementSibling.nextElementSibling`。

**Then**：
- `upperDiv.style.flex === "0 0 auto"`（未拖拽时上区内容自适应，不参与 flex 吸收）
- `sessionDiv.style.flex === "1"`（会话区是弹性吸收区）
- `sessionDiv.style.minHeight === "0"`、`sessionDiv.style.overflowY === "auto"`

---

### 用例 TC-6：office 已拖拽（sessionPanelHeight=300）分区 flex 契约 [P1]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-S4(fixed) + inv-S5(fixed) |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: office + `fixed=true` |
| Oracle | golden value |
| Mock | 见 §4 |

**等价类划分**：`sessionPanelHeight` 非 null → 代表值 `300`（区间内中位值）

**Given**：`seedSidebarState()` 后追加 `usePreferenceStore.setState({ sessionPanelHeight: 300 })`。

**When**：渲染后取 `upperDiv`、`sessionDiv`（定位同 TC-5）。

**Then**：
- `upperDiv.style.flex === "1 1 auto"`、`upperDiv.style.minHeight === "0"`、`upperDiv.style.overflowY === "auto"`（拖拽后上区转为可收缩吸收区）
- `sessionDiv.style.height === "300px"`、`sessionDiv.style.flexShrink === "0"`、`sessionDiv.style.minHeight === "0"`、`sessionDiv.style.overflowY === "auto"`（会话区固定高度，不参与收缩）

> 这是"拖高会话区后 Dock 不被顶出"的关键结构：上区吸收、会话区固定、Dock 不收缩（TC-3）。

---

### 用例 TC-7：coding 默认分区 flex 契约 [P1]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-S6 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: coding + 文件区/会话区结构 |
| Oracle | golden value |
| Mock | 见 §4 |

**等价类划分**：`mode` = `coding`，`sessionPanelHeight` = `null`（默认矮区 180）

**Given**：`seedSidebarState()` 后追加 `usePreferenceStore.setState({ mode: "coding" })`。

**When**：渲染后取 `fileDiv = handle.previousElementSibling`、`sessionDiv = handle.nextElementSibling.nextElementSibling`。

**Then**：
- `fileDiv.style.flex === "1"`、`fileDiv.style.minHeight === "0"`、`fileDiv.style.overflowY === "auto"`（文件区是唯一弹性吸收区）
- `sessionDiv.style.height === "180px"`、`sessionDiv.style.flexShrink === "0"`、`sessionDiv.style.overflowY === "auto"`（会话区退居固定矮区）

---

### 用例 TC-8：拖拽高度钳制 [120,600] 并在 mouseup 提交 [P0]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R4 [P0] / inv-L1 + inv-L3 |
| 测试层级 | component(fake)（真实 ResizeHandle 逻辑经 LeftSidebar 渲染） |
| 覆盖准则 | branch: onMove 的 `Math.min(600, Math.max(120, ...))` 上/下界 + onUp commit |
| Oracle | golden value（钳制公式 + 固定 delta 手算） |
| Mock | 见 §4 |

**等价类划分**：拖拽 delta 使结果 <120（钳到 120）、>600（钳到 600）→ 代表值 `delta=60`、`delta=700`

**Given**：`seedSidebarState()`（mode=office, sessionPanelHeight=null）；`handle`、`sessionDiv` 已定位。
- 确定性控制：jsdom 下 `offsetHeight === 0` → `dragStartH = 0`，故 `h = clamp(0 + delta)`，两个目标高度可精确手算。

**When**：
1. `fireEvent.mouseDown(handle, { clientY: 200 })`（`dragStartY=200`）
2. `act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientY: 140 })))` → delta = 200-140 = 60 → h = 120
3. 断言后继续 `act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientY: -500 })))` → delta = 700 → h = 600
4. `act(() => window.dispatchEvent(new MouseEvent("mouseup")))`

**Then**：
- 步骤 2 后：`sessionDiv.style.height === "120px"`（下界钳制）
- 步骤 3 后：`sessionDiv.style.height === "600px"`（上界钳制）
- 步骤 4 后：`usePreferenceStore.getState().sessionPanelHeight === 600`（钳制后值被提交）

> **jsdom 局限标注**：因 `offsetHeight=0`，`dragStartH` 恒为 0，本用例证明的是「钳制公式的下界 120 与上界 600 都生效」，但**无法证明「从 300 下拖到 50 会钳回 120」的下降钳制**（起点无法预置为 300）。下降钳制由 TC-9 在 store 层补足。

---

### 用例 TC-9：preferenceStore.setSessionPanelHeight 钳制与 null 透传 [P1]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R4 [P1] / inv-L2 |
| 测试层级 | unit（纯 store，不渲染组件） |
| 覆盖准则 | branch: null 透传 / 下界钳制 / 上界钳制 / 边界等值 |
| Oracle | golden value（store 源码 `:106-110` 的钳制公式） |
| Mock | 否——直接测真实 store |

**等价类划分**：输入 height ∈ {null, <120, >600, 120, 600} → 代表值 `null` / `50` / `9999` / `120` / `600`

**Given**：`usePreferenceStore.setState({ sessionPanelHeight: null })`。

**When**：依次调用 `usePreferenceStore.getState().setSessionPanelHeight(v)`。

**Then**：
- `setSessionPanelHeight(50)` → `sessionPanelHeight === 120`（下界）
- `setSessionPanelHeight(9999)` → `=== 600`（上界）
- `setSessionPanelHeight(120)` → `=== 120`（边界等值）
- `setSessionPanelHeight(600)` → `=== 600`（边界等值）
- `setSessionPanelHeight(null)` → `=== null`（复位透传）
- 副作用：无（仅 store 状态）

> 本用例补足 TC-8 无法覆盖的「从高值下拖钳回 120」：`setSessionPanelHeight(50)` 直接验证下界。

---

### 用例 TC-10：拖拽 mousedown 将 target 与上区钉死为拖拽态样式 [P1]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R6 [P1] / inv-L5 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: onMouseDown 的 target 钉死 + `if (upperRef?.current)` 分支（office 传入 upperRef） |
| Oracle | golden value（源码 `:446-455` 命令式赋值） |
| Mock | 见 §4 |

**等价类划分**：office（有 upperRef）且 `sessionPanelHeight=null`（上区初始 `flex:"0 0 auto"`，mousedown 后转为可收缩——变化可归因）

**Given**：`seedSidebarState()`（office, sessionPanelHeight=null）；`handle`、`upperDiv`、`sessionDiv` 已定位。

**When**：`fireEvent.mouseDown(handle, { clientY: 200 })`。

**Then**：
- `sessionDiv.style.flex === "0 0 auto"`（由初始 `"1"` 变为钉死）
- `sessionDiv.style.height === "0px"`（`dragStartH=0` → `` `${0}px` ``）
- `upperDiv.style.flex === "1 1 auto"`（由初始 `"0 0 auto"` 变为可收缩）
- `upperDiv.style.minHeight === "0"`、`upperDiv.style.overflowY === "auto"`

---

### 用例 TC-11：双击 ResizeHandle 复位 sessionPanelHeight 为 null [P2]

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | R7 [P2] / inv-L4 |
| 测试层级 | component(fake) |
| 覆盖准则 | branch: onDoubleClick → onReset |
| Oracle | golden value（`onReset` 传 `setSessionPanelHeight(null)`） |
| Mock | 见 §4 |

**等价类划分**：`sessionPanelHeight` 非 null（fixed）→ 代表值 `300`

**Given**：`seedSidebarState()` 后追加 `usePreferenceStore.setState({ sessionPanelHeight: 300 })`；`handle`、`sessionDiv` 已定位。

**When**：`fireEvent.doubleClick(handle)`（`act` 包裹）。

**Then**：
- `usePreferenceStore.getState().sessionPanelHeight === null`
- 订阅触发重渲染后：`sessionDiv.style.flex === "1"`（回退到弹性吸收态，`sessionDiv.style.height === ""`）

---

## 8. jsdom 无法覆盖 · 需 playwright/人工验证清单

以下**不写进 vitest**，明确标注为缺口（R8），需真实浏览器验证：

| # | 待验证行为 | 为什么 jsdom 覆盖不了 | 建议手段 |
|---|-----------|----------------------|---------|
| P-1 | 拖高会话区（固定 height+flexShrink:0）+ 窗口非最大化（矮 viewport）→ **SidebarDock（登录/设置/退出）仍完整在可视区** | jsdom 不计算 flex 布局，offsetHeight/clientHeight 恒 0 | playwright：设 `viewport = { height: 500 }`，触发 ResizeHandle 拖到 600px，断言「退出登录」按钮 `boundingBox` 在 viewport 内且未与底部重叠 |
| P-2 | 溢出被 bodyRoot 的 `overflow:hidden` 裁剪在容器内，**不泄漏到 Dock** | jsdom 无滚动/裁剪计算 | playwright 截图对比或断言 Dock 元素与 bodyRoot 无视觉重叠 |
| P-3 | ModeSwitcher/MainNav 因默认 `min-height:auto` 不可压缩时，整体高度不再溢出根容器 | 同上 | playwright：极小 viewport + 拖拽至 600，断言根容器 `scrollHeight <= clientHeight`（无溢出） |
| P-4 | coding 模式同样不顶出 Dock（修复对两模式都生效的视觉后果） | 同上 | playwright：切 coding，拖拽会话区，断言 Dock 可见 |

---

## 9. Known-Gap

无。当前实现已应用修复（`bodyStyle` 包装存在于 office/coding 两分支），设计期望与实现一致，不产生 `[known-gap]` 标记。

## 10. 交付边界

- 本设计仅覆盖**功能测试**（结构不变式 + 可单元化拖拽/store 逻辑）。
- 性能/可访问性（键盘拖拽、屏幕阅读器焦点）不在范围内，如需请转专项。
- 用例按 P0→P2 排列，资源有限时先保 TC-1/2/3/8 四个 P0。
