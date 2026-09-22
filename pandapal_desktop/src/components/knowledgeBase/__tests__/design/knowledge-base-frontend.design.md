# 测试设计：知识库文件树 / 预览 / 结构操作 前端改造

> 被测目标（相对 `pandapal_desktop`）
> - `src/components/knowledgeBase/treeUtils.ts`（纯函数）
> - `src/components/knowledgeBase/KBFileTree.tsx`（递归树 / 搜索 / 重命名 / 拖拽）
> - `src/components/knowledgeBase/KBPreviewPanel.tsx`（`previewMode` / `textRendererKind` + 预览组件）
> - `src/components/knowledgeBase/KBContextMenu.tsx`、`ConfirmDialog.tsx`、`KBDropZone.tsx`（`resolveDropDirAt`）
> - `src/store/kbStore.ts`（`selectedPath` / `rightTab` / `remove` / `reset`）
> - `src/pages/KnowledgeBaseEditorPage.tsx`（编辑模式双栏 + 菜单动作 → IPC）
> - `src/providers/BackendProvider.tsx`（`KB_ERROR_MESSAGES` 错误码 → toast）
>
> 测试栈：vitest + @testing-library/react + jsdom（`vitest.config.ts`，`setupFiles: src/test/setup.ts`，`include: src/**/*.test.{ts,tsx}`）

## 0. 范围声明

本设计聚焦**功能行为与结构不变式**。性能（>1MB 闸门只验证"是否触发闸门"，不做读取耗时断言）、安全（路径穿越由后端 `invalid_path` 兜底，前端只验错误码映射）等非功能项**只做识别与标注**，不展开专项用例。

jsdom 限制：真实操作系统文件拖拽（Tauri `onDragDropEvent`）无法在 jsdom 复现 → `KBDropZone` 只测 **`resolveDropDirAt` 纯函数** + **`data-drop-dir` 属性接线**（用例 40–43），真实落盘链路转人工/playwright。

---

## 1. 确定性控制（设计阶段即钉死，防 flaky）

| 不确定源 | 对策（写进 Given） |
|---|---|
| i18n | `beforeEach` `await i18n.changeLanguage("zh-CN")`；断言用 zh-CN golden 值或源码内 fallback 字面量 |
| `kbStore` 单例 | 每例 `beforeEach` 调 `useKbStore.getState().reset()`（用例 44–48 自行 `setState` 种子） |
| `Date.now()`（renameRequest.token） | `vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000)`，token 固定 |
| `window.innerWidth/innerHeight`（菜单定位） | jsdom 默认 1024×768；断言不依赖像素坐标，只断言菜单项集合 |
| `document.elementFromPoint`（落点解析） | jsdom 未实现 → `vi.spyOn(document,"elementFromPoint")` 钉死返回目标元素 |
| `devicePixelRatio` | jsdom 默认 1，无需额外 stub（`resolveDropDirAt` 纯函数不走它） |
| 浮点/时间戳 | 不涉及；`size` 用整数字节 |

---

## 2. Mock / Fake 策略

沿用项目既有 `settingsPanelCrash.test.tsx`（真实渲染 + 只 mock Tauri IPC）与 `InteractionInline.test.tsx`（mock `useBackend`）模式。

| 被测目标 | 依赖 | 决策 | 理由 |
|---|---|---|---|
| `treeUtils` / `previewMode` / `textRendererKind` / `resolveDropDirAt` | 无 | **零 mock**（`resolveDropDirAt` 仅 stub `document.elementFromPoint`） | 纯函数，直接断言 |
| `KBPreviewPanel` | `@tauri-apps/plugin-fs` `readTextFile` | **mock** `vi.fn()` | jsdom 无 Tauri 运行时；需断言"是否读盘/读哪个路径" |
| | `@tauri-apps/api/path` `join` | **mock** `vi.fn((a,b)=>`${a}/${b}`)` | 需断言 join 的入参（P0 身份口径） |
| | `../fileRenderers`（`MarkdownRenderer` 等 6 个 + `fileIcon`/`toMonacoLang`） | **mock** 为轻量 stub（渲染 `data-testid` + `content`） | 避免 Monaco 重依赖；便于断言 content 透传 |
| | `react-i18next` | **真实** i18n（`import "../../../i18n"`） | 与既有测试一致；文本用 zh-CN golden |
| `KBFileTree` | `../fileRenderers` `fileIcon` | **mock**（返回原名文本） | 图标依赖隔离 |
| `KBContextMenu` / `ConfirmDialog` | 无 IO | **真实渲染**，仅真实 i18n | 纯展示 + 回调 |
| `KBDropZone` | `@tauri-apps/api/webviewWindow` `getCurrentWebviewWindow` | **mock**（`onDragDropEvent` 返回 `Promise.resolve(noop)`） | jsdom 无 Tauri；只防导入崩 |
| `kbStore` | — | **真实 store** | 无 IO |
| `KnowledgeBaseEditorPage` | `useBackend()` | **mock**（`vi.hoisted` + `vi.fn()`：`requestKbDetail / requestKbTree / createKb / saveKb / uploadKbDocument / deleteKbDocument / buildKb / cancelBuild / searchKb / createKbFolder / renameKbFolder / deleteKbFolder / renameKbDocument / moveKbDocument`） | 不挂真实 `BackendProvider`（其 effect 会 listen/invoke）；解构键必须齐全防 undefined 崩溃 |
| | `@tauri-apps/plugin-dialog` `open` | **mock** `vi.fn()` | `pickAndUpload` 外部选择器 |
| | `react-router-dom` | **真实** `<MemoryRouter initialEntries={["/knowledge/MyKB/edit"]}>` + `<Routes><Route path="/knowledge/:name/edit">` | jsdom 可用 |
| | `kbStore` / `i18n` | **真实** | 端到端串起"菜单动作 → store/IPC" |
| `KB_ERROR_MESSAGES` | — | 见 **Known-Gap G1**（当前非导出） | 需 `export` 后方可静态断言 |

**`useBackend` stub 键集**：必须覆盖上表 14 个 KB 方法（缺一即 `undefined` 调用崩溃）；`sendSessionIpc` 契约由这些方法内部使用，测试层只断言方法入参。

---

## 3. 风险清单（RISK 分级，S 严重度 × L 可能性）

| ID | 风险 | S | L | 优先级 | 覆盖用例 |
|----|------|---|---|--------|---------|
| R1 | 预览读盘未 `join(documentsDir, node.path)`（用相对 path 直接读）→ 读错文件/读不到 | 高 | 中 | **P0** | 12、16 |
| R2 | `docx/doc/二进制` 落入文本兜底 → Monaco 乱码 | 中 | 中 | **P0** | 9、15 |
| R3 | 多层路径父目录/祖先解析错（`findParentPath` / `targetDirOf` / 文件行 `data-drop-dir`） | 高 | 中 | **P0** | 4、7、26、41、57 |
| R4 | `filterTree` 祖先链丢失 / 大小写敏感 / 空查询未排序 | 中 | 中 | **P1** | 6、7、8、20 |
| R5 | 重命名空名 / 同名仍提交 → 后端 `invalid_name`/`name_conflict` | 中 | 高 | **P1** | 22、23、24 |
| R6 | 右键菜单项随 文件/文件夹、根/非根 错配 | 中 | 中 | **P1** | 30、31、32 |
| R7 | `ConfirmDialog` `open=false` 仍渲染 / 确认取消回调错配 | 中 | 中 | **P1** | 36、37、38、39、50–52 |
| R8 | 落点解析错（文件应落父目录）→ 上传/移动进错目录 | 中 | 中 | **P1** | 41、43、57 |
| R9 | `kbStore.remove/reset` 未回收 → 跨库残留（树/上传反馈/进度串库） | 中 | 中 | **P1** | 46、47、48 |
| R10 | 错误码映射缺失/串值 → 用户看不到正确提示 | 中 | 中 | **P1** | 49 |
| R11 | 预览读盘失败未兜底 → 组件崩溃 / 白屏 | 中 | 低 | **P1** | 17 |
| R12 | 边界：空树、>1MB、whitespace 查询、文件夹计数 | 低 | 中 | **P2** | 2、6、13、19 |

---

## 4. 不变式清单

| ID | 不变式 | 目标 |
|----|--------|------|
| inv-1 | `sortNodes` 返回新数组，目录优先 + 组内 `name.localeCompare(name,"zh")` 升序 | treeUtils |
| inv-2 | `countFiles` 只计文件；`countFolders` 只计目录（根不计入自身）；`null/undefined → 0` | treeUtils |
| inv-3 | `findNode` 按 path 精确命中（同级同名 basename 由完整 path 区分），未命中 `null` | treeUtils |
| inv-4 | `findParentPath`：根项 `""`、多层返回父相对路径、未命中 `null` | treeUtils |
| inv-5 | `subtreeHasMatch`：自身或后代 name 子串命中，大小写不敏感 | treeUtils |
| inv-6 | `filterTree`：命中保留祖先链；`rawQuery.trim()===""` → 全量（已排序）+ 空 set；`expandPaths` = 命中目录集；无命中 → `[]` | treeUtils |
| inv-7 | `previewMode`：`pdf→pdf`；`png/jpg/jpeg/gif/webp/svg/bmp/ico→image`；`md/markdown/txt/log/html/htm/csv/tsv/json→text`；其余（含 `doc/docx/空`）→ `degraded` | preview |
| inv-8 | `textRendererKind`：`md/markdown→md`、`txt/log→log`、`html/htm→html`、`csv/tsv→table`、其余 → `code` | preview |
| inv-9 | `doc===null` → 空态，不 `join`/不读盘 | KBPreviewPanel |
| inv-10 | `degraded` → 降级文案，不 `join`/不读盘 | KBPreviewPanel |
| inv-11 | 文本类：`join(documentsDir, path)` → `readTextFile(absPath)`；`size > MAX_TEXT_BYTES` → 只提示"过大"，不读盘 | KBPreviewPanel |
| inv-12 | `documentsDir === ""` → 提示"未配置"，不读盘 | KBPreviewPanel |
| inv-13 | 读盘 reject → 展示错误文案，不抛渲染异常 | KBPreviewPanel |
| inv-14 | 搜索态仅显示命中节点及祖先链；清空恢复全量；无命中显示"无匹配文件" | KBFileTree |
| inv-15 | 重命名：`Enter` 提交（trim 后非空且 ≠ 原名才 `onRename`）；`Escape` 取消不提交 | KBFileTree |
| inv-16 | 文件夹默认收起；点击/双击/箭头 toggle | KBFileTree |
| inv-17 | 树内拖拽：落点目录 = 文件夹自身 path / 文件父目录 / 容器 `""`；`src === targetDir` 不触发 | KBFileTree |
| inv-18 | 底部统计 = `countFiles(tree)` / `countFolders(tree)`；上传反馈按 uploaded/rejected 渲染 | KBFileTree |
| inv-19 | 菜单项：文件含「预览」，文件夹不含；`path` 含 `"/"` 才含「移到根目录」；删除项为 danger | KBContextMenu |
| inv-20 | 菜单点击项 → `onAction(action)` 后 `onClose()`；`Esc` / 外部点击 → `onClose()` | KBContextMenu |
| inv-21 | `open===false` → 返回 `null`；`open===true` → 渲染 dialog，确认/取消/遮罩回调正确 | ConfirmDialog |
| inv-22 | `resolveDropDirAt`：命中 folder 行 → 其 path；命中 file 行 → 父目录；命中容器 → `""`；无命中 → `null` | KBDropZone |
| inv-23 | `selectedPath` 初始 `null`、`setSelectedPath` 生效、`reset` 清空 | kbStore |
| inv-24 | `rightTab` 初始 `"preview"`、`setRightTab` 生效、`reset` 回 `"preview"` | kbStore |
| inv-25 | `remove(name)` 回收该库 `treeByName/uploadFeedbackByName/buildByName`（保留其他库） | kbStore |
| inv-26 | `KB_ERROR_MESSAGES` 8 码 → 8 条互异非空中文文案 | BackendProvider |
| inv-27 | 菜单动作 → 正确 IPC：`preview→setSelectedPath+setRightTab`、`move-to-root→moveKbDocument(kb,path,"")`、`delete→二次确认→deleteKb*`、`new-folder→createKbFolder`、`upload-here→targetDirOf` | EditorPage |
| inv-28 | `selectedPath` 指向文件夹 → `previewDoc === null` | EditorPage |

---

## 5. 覆盖矩阵：风险 / 不变式 → 用例

**风险 → 用例（含等级）**

| 风险 | 等级 | 覆盖用例 |
|------|------|---------|
| R1 预览路径身份口径 | P0 | 12、16 |
| R2 二进制乱码 | P0 | 9、15、16 |
| R3 多层父目录解析 | P0 | 4、7、26、41、57 |
| R4 filterTree 语义 | P1 | 6、7、8、20 |
| R5 重命名脏数据 | P1 | 22、23、24 |
| R6 菜单项错配 | P1 | 30、31、32、33 |
| R7 ConfirmDialog 契约 | P1 | 36、37、38、39、50、51、52 |
| R8 落点解析 | P1 | 41、43、57 |
| R9 store 回收 | P1 | 46、47、48 |
| R10 错误码映射 | P1 | 49 |
| R11 读盘失败兜底 | P1 | 17 |
| R12 边界 | P2 | 2、6、13、19 |

**不变式 → 用例**

| 不变式 | 用例 | 不变式 | 用例 |
|--------|------|--------|------|
| inv-1 | 1 | inv-15 | 22、23、24 |
| inv-2 | 2、19 | inv-16 | 21 |
| inv-3 | 3 | inv-17 | 25、26、27、28 |
| inv-4 | 4 | inv-18 | 19、29 |
| inv-5 | 5 | inv-19 | 30、31、32、33 |
| inv-6 | 6、7、8 | inv-20 | 34、35 |
| inv-7 | 9 | inv-21 | 36、37、38、39 |
| inv-8 | 10 | inv-22 | 40、41、42、43 |
| inv-9 | 11 | inv-23 | 44、46 |
| inv-10 | 15 | inv-24 | 45、46 |
| inv-11 | 12、13、18 | inv-25 | 47 |
| inv-12 | 14 | inv-26 | 49 |
| inv-13 | 17 | inv-27 | 50、51、52、53、54、55、56、57、59 |
| inv-14 | 20 | inv-28 | 58 |

**用例 × 风险等级分布**：P0 = 9 例（4、7、9、12、15、16、26、41、57）、P2 = 4 例（2、6、13、19）、其余 46 例为 P1，共 **59 例**。

---

## 6. 用例详情

### A 组：treeUtils（unit，零 mock）

---

#### 用例1：sortNodes 目录优先 + zh 升序 + 不改原数组

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-1 [P1] |
| 测试层级 | unit |
| 覆盖准则 | N/A（单表达式无分支；仅 `is_dir` 判定分支） |
| Oracle | golden value（`localeCompare("zh")` 固定 collation，可独立推导） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：节点类型 ∈ {目录, 文件} × 名称大小写 ∈ {大写首字母, 小写} → 代表值 = 目录 `["Alpha","beta"]` + 文件 `["a.md","b.md"]`（混合入参，故意打乱输入顺序）

**Given**：`tree = [ {name:"b.md",is_dir:false,path:"b.md"}, {name:"beta",is_dir:true,path:"beta"}, {name:"Alpha",is_dir:true,path:"Alpha"}, {name:"a.md",is_dir:false,path:"a.md"} ]`
**When**：`const out = sortNodes(tree)`
**Then**：
- 返回值：`out.map(n=>n.name)` = `["Alpha","beta","a.md","b.md"]`（目录组在前，组内 ASCII/拼音升序）
- 副作用：`out !== tree`（新数组）；`tree[0].name === "b.md"`（原数组未被排序）
- 无副作用，仅验证返回值与入参不可变

---

#### 用例2：countFiles / countFolders 递归计数与 null 边界

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-2 [P1] + R12 边界 [P2] |
| 测试层级 | unit |
| 覆盖准则 | N/A（递归分支：`is_dir` 真/假；空集合分支） |
| Oracle | golden value（手算） |
| Mock | 否 — 纯函数零 mock |

**等价类划分**：输入 ∈ {空 `null`, 空 `undefined`, 空数组, 单文件, 单目录, 嵌套目录} → 代表值 = 嵌套树

**Given**：`tree = [ {is_dir:true,path:"docs",children:[ {is_dir:false,...}, {is_dir:true,path:"docs/sub",children:[{is_dir:false,...}]} ]}, {is_dir:false,path:"c.md"} ]`
**When/Then**：
- `countFiles(tree)` = `3`（docs/a、docs/sub/b、c.md）
- `countFolders(tree)` = `2`（docs、docs/sub；根不计入自身）
- 边界：`countFiles(null)` = `0`、`countFolders(undefined)` = `0`、`countFiles([])` = `0`

---

#### 用例3：findNode 完整 path 区分同名 basename

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-3 [P1] + R3 多层路径 [P0] |
| 测试层级 | unit |
| 覆盖准则 | N/A（DFS 命中/未命中分支） |
| Oracle | golden value（按 path 精确匹配） |
| Mock | 否 |

**等价类划分**：查找路径 ∈ {根项, 多层嵌套项, 非根同名 basename, 不存在} → 代表值见下

**Given**：`tree = [ {is_dir:true,path:"x",children:[{is_dir:false,path:"x/notes.md",name:"notes.md"}]}, {is_dir:true,path:"y",children:[{is_dir:false,path:"y/notes.md",name:"notes.md"}]} ]`
**When/Then**：
- `findNode(tree,"y/notes.md")!.path` = `"y/notes.md"`（不是 `x/` 下同名项）
- `findNode(tree,"notes.md")` = `null`（不存在该根路径项 → 证明按完整 path 而非 basename 匹配）
- `findNode(tree,"ghost.md")` = `null`

---

#### 用例4：findParentPath 根 `""` / 多层父路径 / 未命中

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-4 [P1] + R3 多层路径 [P0] |
| 测试层级 | unit |
| 覆盖准则 | N/A（递归 `parent` 累积；未命中分支） |
| Oracle | golden value（POSIX 父路径可手推） |
| Mock | 否 |

**等价类划分**：目标路径层级 ∈ {根, 一层, 两层, 不存在} → 代表值 = `docs`（根）、`docs/a.md`（一层）、`docs/sub/b.md`（两层）、`ghost`（不存在）

**Given**：`tree = [ {is_dir:true,path:"docs",children:[ {is_dir:false,path:"docs/a.md"}, {is_dir:true,path:"docs/sub",children:[{is_dir:false,path:"docs/sub/b.md"}]} ]} ]`
**When/Then**：
- `findParentPath(tree,"docs")` = `""`（根项父目录为空串）
- `findParentPath(tree,"docs/a.md")` = `"docs"`
- `findParentPath(tree,"docs/sub/b.md")` = `"docs/sub"`（分母目录 basename 重名不干扰）
- `findParentPath(tree,"ghost")` = `null`

---

#### 用例5：subtreeHasMatch 大小写不敏感 + 后代命中

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-5 [P1] |
| 测试层级 | unit |
| 覆盖准则 | N/A（自身命中 / 后代命中 / 均未命中三路） |
| Oracle | golden value（`toLowerCase().includes` 语义） |
| Mock | 否 |

**等价类划分**：查询串 ∈ {自身名子串, 后代名子串, 无命中}（调用方已 lowercase）→ 代表值 = `"docs"`、`"readme"`、`"zzz"`

**Given**：`dir = {is_dir:true,name:"Docs",children:[{is_dir:false,name:"ReadMe.md",children:null}]}`；`file = {is_dir:false,name:"a.md",children:null}`
**When/Then**：
- `subtreeHasMatch(dir,"docs")` = `true`（自身、大小写不敏感）
- `subtreeHasMatch(dir,"readme")` = `true`（后代命中）
- `subtreeHasMatch(dir,"zzz")` = `false`
- `subtreeHasMatch(file,"a")` = `true`；`subtreeHasMatch(file,"docs")` = `false`（文件无 children，不递归）

---

#### 用例6：filterTree 空查询 / 纯空白查询 → 全量已排序 + 空 expandPaths

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 [P1] + R4 + R12 边界 [P2] |
| 测试层级 | unit |
| 覆盖准则 | 分支：`if (!q) return ...` 真分支 |
| Oracle | golden value（返回全量且目录优先） |
| Mock | 否 |

**等价类划分**：查询 ∈ {`""`, `"   "`（纯空白）} → 代表值 = `""`、`"   "`

**Given**：`tree` = 用例7 的多层树（4 个顶级节点）
**When/Then**：对 `""` 与 `"   "` 各调一次：
- 返回值：`res.nodes.length === 4`，`res.nodes.map(n=>n.name)` 目录优先排序
- 副作用：`res.expandPaths.size === 0`
- `res.nodes !== tree`（`sortNodes` 产生新数组）

---

#### 用例7：filterTree 命中保留祖先链 + 大小写不敏感 + 无命中空数组

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 [P1] + R4 + R3 多层路径 [P0] |
| 测试层级 | unit |
| 覆盖准则 | 分支：`subtreeHasMatch` 真/假；`n.is_dir` 真/假；递归 walk |
| Oracle | golden value（结构 + expandPaths 集可手推） |
| Mock | 否 |

**等价类划分**：查询 ∈ {命中深层文件（大小写异）, 命中目录名, 无命中} → 代表值 = `"REPORT"`、`"photos"`、`"zzz"`

**Given**：`tree = [ {is_dir:true,path:"docs",children:[ {is_dir:true,path:"docs/sub",children:[{is_dir:false,path:"docs/sub/report.md",name:"report.md"}]}, {is_dir:false,path:"docs/other.md",name:"other.md"} ]}, {is_dir:true,path:"photos",children:[{is_dir:false,path:"photos/pic.png",name:"pic.png"}]} ]`
**When/Then**：
- 查 `"REPORT"`：`nodes.length===1` 且 `nodes[0].path==="docs"`；`docs.children` 只剩 `sub`；`sub.children` 只剩 `report.md`；`expandPaths` = `Set{"docs","sub"}`；`other.md` / `photos` 已被剔除
- 查 `"photos"`：`nodes` = `[photos]`（仅命中分支），`expandPaths` = `Set{"photos"}`
- 查 `"zzz"`：`nodes === []`，`expandPaths.size === 0`（无命中 → 空）

---

#### 用例8：filterTree 命中目录自身名时子链被裁剪

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-6 [P1] + R4 |
| 测试层级 | unit |
| 覆盖准则 | 分支：目录自身命中但无子命中 → `children` 为空数组 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：查询命中目录名但不命中任何子项 → 代表值 = `"reports"`

**Given**：`tree = [ {is_dir:true,path:"reports",children:[{is_dir:false,path:"reports/a.md",name:"a.md"}]} ]`
**When/Then**：
- `nodes.length===1`，`nodes[0].path==="reports"`
- `nodes[0].children` = `[]`（子项未命中被过滤，但目录本身因自身命中而保留）
- `expandPaths` = `Set{"reports"}`

---

### B 组：previewMode / textRendererKind（unit，零 mock）

---

#### 用例9：previewMode 后缀 → 预览模式全量分派

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-7 [P1] + R2 二进制乱码 [P0] |
| 测试层级 | unit |
| 覆盖准则 | 分支：`!ext` / `pdf` / `IMAGE` / `TEXT` / else，全覆盖 |
| Oracle | golden value（源码 Set 白纸黑字 + 规格 §3 写死） |
| Mock | 否 |

**等价类划分**：后缀 ∈ {空, pdf, 各图片扩展, 各文本扩展, doc/docx, 未知二进制, 带点大写} → 代表值见下

**Given**：无前置
**When/Then**（表驱动）：
- `previewMode("pdf")==="pdf"`；`previewMode(".PDF")==="pdf"`
- `previewMode("png"|"jpg"|"jpeg"|"gif"|"webp"|"svg"|"bmp"|"ico")==="image"`
- `previewMode("md"|"markdown"|"txt"|"log"|"html"|"htm"|"csv"|"tsv"|"json")==="text"`；`previewMode("Md")==="text"`
- `previewMode("docx"|"doc"|"xls"|"zip"|"bin"|"unknown")==="degraded"`；`previewMode("")==="degraded"`；`previewMode(".DocX")==="degraded"`（**证明 docx 绝不落 text**）

---

#### 用例10：textRendererKind 文本子类 → 渲染器标识

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-8 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：MD / LOG / HTML / TABLE / else，全覆盖 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：文本后缀 ∈ {md 系, log 系, html 系, 表格式, 其它} → 代表值见下

**When/Then**：
- `"md","markdown"` → `"md"`；`"txt","log"` → `"log"`；`"html","htm"` → `"html"`；`"csv","tsv"` → `"table"`
- `"json","xml","py","yaml",""` → `"code"`（其它兜底为 code）

---

### C 组：KBPreviewPanel（component，mock fs/path/renderers）

> 统一 mock：`join` → `` `${a}/${b}` ``；`readTextFile` → `vi.fn()`；`MarkdownRenderer` 等 stub 渲染 `<div data-testid="md-renderer">{content}</div>`（其余同理）。

---

#### 用例11：doc=null → 空态且不触发读盘

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-9 [P1] + R12 边界 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`if (!doc) return` |
| Oracle | golden value（源码硬编码文案） |
| Mock | 是 — `join`/`readTextFile`/renderers stub |

**Given**：`render(<KBPreviewPanel doc={null} documentsDir="/kb/demo"/>)`
**Then**：
- 文本：`"从左侧文件树选择一个文档进行预览"` 可见
- 副作用：`join` 未被调用、`readTextFile` 未被调用

---

#### 用例12：文本类 ≤1MB → join(documentsDir, path) 后 readTextFile(abs) 【P0 身份口径】

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-11 [P1] + R1 路径身份 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`mode==="text"` 且 `size<=MAX_TEXT_BYTES` |
| Oracle | golden value（join 入参契约 = "相对 documents_dir 的 path"，规格 §1 写死） |
| Mock | 是 — fs/path/renderers |

**等价类划分**：doc 路径层级 ∈ {根文件, 子目录文件} → 代表值 = `path:"docs/sub/a.md"`

**Given**：`readTextFile` mock 返回 `"# 标题"`；`doc = {path:"docs/sub/a.md", name:"a.md", suffix:".md", size:5000}`；`documentsDir="/kb/d1"`
**When**：`render(...)`，`await waitFor(()=> expect(getByTestId("md-renderer")).toBeTruthy())`
**Then**：
- 副作用（关键）：`join` 被调用且参数 = `("/kb/d1", "docs/sub/a.md")`（**相对 path，非绝对**）
- 副作用：`readTextFile` 被调用且参数 = `"/kb/d1/docs/sub/a.md"`（join 结果）
- 返回值：`md-renderer` 的 textContent = `"# 标题"`（content 透传）

---

#### 用例13：文本类 1MB 闸门边界对（=MAX 读、>MAX 不读）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-11 [P1] + R12 边界 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`doc.size > MAX_TEXT_BYTES` 真/假（边界 +1） |
| Oracle | golden value（阈值 `MAX_TEXT_BYTES` 源码导出，可手算） |
| Mock | 是 |

**等价类划分**：size ∈ {`MAX_TEXT_BYTES`(1048576), `MAX_TEXT_BYTES+1`(1048577)} → 边界对

**Given**：`doc = {path:"a.txt", name:"a.txt", suffix:".txt", size: <上值>}`；`documentsDir="/kb/d1"`
**When/Then**：
- `size = 1048576`：`readTextFile` 被调用一次，内容渲染（**边界内**）
- `size = 1048577`：展示 `"文件过大，无法预览（超过 1MB）"`，且 `readTextFile` **未被调用**（**边界外**）

---

#### 用例14：documentsDir 为空 → 提示未配置，不读盘

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-12 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`if (!documentsDir)` |
| Oracle | golden value（源码 fallback 文案） |
| Mock | 是 |

**Given**：`render(<KBPreviewPanel doc={{path:"a.md",name:"a.md",suffix:".md",size:10}} documentsDir=""/>)`
**Then**：文本 `"未配置文档目录，无法预览"` 可见；`join` 与 `readTextFile` **均未被调用**（早于 join 返回）

---

#### 用例15：degraded（docx）→ 降级文案，不 join 不读盘 【P0 乱码防护】

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-10 [P1] + R2 二进制乱码 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`mode==="degraded"`（早退，不进入文本兜底） |
| Oracle | golden value（源码文案） |
| Mock | 是 |

**等价类划分**：degraded 后缀 ∈ {`docx`, `doc`, 未知二进制} → 代表值 = `.docx`（`.doc`/`bin` 由用例9 覆盖）

**Given**：`doc = {path:"契约.docx", name:"契约.docx", suffix:".docx", size:8000}`；`documentsDir="/kb/d1"`
**When**：`render(...)`
**Then**：
- 文本：`"暂不支持预览此格式，请在系统中打开"` 可见；文件名 `"契约.docx"` 展示于 header/降级区
- 副作用：`join` **未被调用**、`readTextFile` **未被调用**、`CodeRenderer/MarkdownRenderer` 等文本渲染器**均未渲染**（无乱码）

---

#### 用例16：pdf / image → 组件自读盘，join 还原绝对路径

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-11 邻接 + R1 路径身份 [P0] + R2 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`mode==="pdf"` / `mode==="image"` |
| Oracle | golden value（join 入参 + 渲染器 path 透传） |
| Mock | 是（`PdfRenderer`/`ImageRenderer` stub 渲染 `data-testid` + `path`） |

**等价类划分**：媒体类型 ∈ {pdf, image} → 代表值 = `.pdf`、`.png`

**Given**：`documentsDir="/kb/d1"`；doc suffix `.pdf`（再 `.png`）
**When/Then**：
- `.pdf`：`join` 调用参数 = `("/kb/d1","a.pdf")`；`PdfRenderer` 收到 `path="/kb/d1/a.pdf"`；`readTextFile` **未调用**
- `.png`：`ImageRenderer` 收到 `path="/kb/d1/a.png"`；`readTextFile` **未调用**

---

#### 用例17：读盘失败 → 展示错误，组件不崩溃 【故障注入】

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-13 [P1] + R11 读盘失败 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`catch(e)` |
| Oracle | golden value（`String(e)` 透传） |
| Mock | 是 |

**故障注入**：故障类型 = 文件读取权限/IO 失败；注入点 = `readTextFile` reject；预期行为 = 静默捕获 → `setError(String(e))` 展示，`render` 不抛异常

**Given**：`readTextFile` mock `mockRejectedValue(new Error("EACCES"))`；doc = `.txt` 且 size 正常
**When**：`render(...)`，`await waitFor(...)`
**Then**：
- 文本含 `"EACCES"`（error 分支渲染）
- 渲染过程无未捕获异常（测试不 fail）

---

#### 用例18：切换 doc → 重新读盘并清空旧内容

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-11 [P1]（effect 依赖 `doc?.path`） |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：effect 依赖变化重跑 + `cancelled` 清理 |
| Oracle | golden value（join 被以新 path 再次调用） |
| Mock | 是 |

**Given**：`dashboard` 先渲染 doc `a.md`（readTextFile → `"A"`）；`rerender` 为 doc `b.md`（readTextFile → `"B"`）
**When/Then**：
- `readTextFile` 第二次调用参数 = `/kb/d1/b.md`（新绝对路径）
- 渲染内容最终 = `"B"`（旧 `"A"` 被清空，无残留）

---

### D 组：KBFileTree（component，mock fileRenderers.fileIcon）

---

#### 用例19：空树 → 空态 + 统计 0

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-18 [P1] + R12 边界 [P2] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`viewNodes.length === 0` 且非搜索 |
| Oracle | golden value（源码文案） |
| Mock | 是 — `fileIcon` stub |

**Given**：`render(<KBFileTree tree={[]} searchQuery="" .../>)`
**Then**：
- 文本：`"还没有文件，拖拽或上传文档开始"` 可见
- 文本：`"0 个文档"`、`"0 个文件夹"` 可见

---

#### 用例20：搜索过滤仅显命中及祖先链 / 清空恢复全量 / 无命中提示

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-14 [P1] + R4 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`searching` 真/假；`viewNodes.length===0 && searching` |
| Oracle | golden value（受控 `searchQuery` 驱动 `filterTree`） |
| Mock | 是 |

**等价类划分**：查询 ∈ {命中深层, 无命中, 清空} → 代表值 = `"report"`、`"zzz"`、`""`

**Given**：tree = 用例7 的多层树；受控渲染（`searchQuery` 由 wrapper state 驱动，`onSearchChange` 更新）
**When/Then**：
- 输入 `"report"`：`kb-node-docs/sub/report.md` 存在；`queryByTestId("kb-node-photos/pic.png")` = `null`（祖先链保留、无关分支剔除）
- 输入 `"zzz"`：`"无匹配文件"` 可见，所有 `kb-node-*` 均不存在
- 输入清空 `""`：`kb-node-photos/pic.png`、`kb-node-docs/other.md` 恢复可见（全量）

---

#### 用例21：文件夹默认收起，点击展开/再点收起

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-16 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`expandedNow` 真/假 |
| Oracle | golden value（子节点 testid 可见性） |
| Mock | 是 |

**Given**：tree 含 `docs`（有子 `docs/a.md`），默认 `expanded` 空
**When**：`fireEvent.click(getByTestId("kb-node-docs"))` → 再点一次
**Then**：
- 初始：`queryByTestId("kb-node-docs/a.md")` = `null`（收起）
- 点击后：`getByTestId("kb-node-docs/a.md")` 存在（展开）；`onSelect` 被调用 1 次
- 再点击：子节点不可见（收起）

---

#### 用例22：内联重命名 Enter 提交（trim 后）→ onRename(node, newName)

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-15 [P1] + R5 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`e.key==="Enter"` + `commitRename` 有效提交路径 |
| Oracle | golden value（回调入参） |
| Mock | 是 |

**Given**：tree 含 `a.md`；`renameRequest={{path:"a.md", token:1}}`
**When**：`await waitFor(()=>getByTestId("kb-rename-input-a.md"))`；`fireEvent.change(input,{target:{value:"  new.md  "}})`；`fireEvent.keyDown(input,{key:"Enter"})`
**Then**：
- 副作用：`onRename` 被调用 1 次，参数 = `(node_a_md, "new.md")`（**已 trim**）
- 输入框消失（`renamingPath` 复位）

---

#### 用例23：重命名空名 / 纯空白 / 同名 → 不触发 onRename

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-15 [P1] + R5 重命名脏数据 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`!v \|\| v === node.name` → 提前 return |
| Oracle | golden value（回调未被调用） |
| Mock | 是 |

**等价类划分**：输入值 ∈ {`""`, `"   "`, 与原同名 `"a.md"`} → 三个无效代表值

**Given**：同上，`renameRequest` token 递增以重开输入（每个代表值独立 render）
**When**：分别输入 `""` / `"   "` / `"a.md"` 后 `keyDown Enter`
**Then**：三种情况 `onRename` **均未被调用**；输入框均消失

---

#### 用例24：重命名 Escape → 取消，不触发 onRename

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-15 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`e.key==="Escape"` |
| Oracle | golden value |
| Mock | 是 |

**Given**：tree 含 `a.md`，`renameRequest={{path:"a.md",token:1}}`，输入框出现
**When**：`fireEvent.change(input,{target:{value:"changed.md"}})`；`fireEvent.keyDown(input,{key:"Escape"})`
**Then**：`onRename` 未被调用；输入框消失；行文本仍为 `a.md`

---

#### 用例25：树内拖拽 drop 到文件夹行 → onMoveNode(src, folderPath)

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-17 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`isFolder` 时 `dropTargetDir = node.path` |
| Oracle | golden value（回调入参） |
| Mock | 是 |

**Given**：tree 含根文件 `a.md` 与文件夹 `docs`（展开）；`dataTransfer` stub = `{setData:vi.fn(), effectAllowed:"", dropEffect:""}`
**When**：`fireEvent.dragStart(getByTestId("kb-node-a.md"), {dataTransfer})`；`fireEvent.dragOver(getByTestId("kb-node-docs"),{dataTransfer})`；`fireEvent.drop(getByTestId("kb-node-docs"),{dataTransfer})`
**Then**：副作用：`onMoveNode` 调用 1 次，参数 = `("a.md", "docs")`

---

#### 用例26：树内拖拽 drop 到文件行 → onMoveNode(src, 父目录) 【P0 身份口径】

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-17 [P1] + R3/R8 多层父目录 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`isFolder=false` 时 `dropTargetDir = parentPath` |
| Oracle | golden value（父目录相对路径可推） |
| Mock | 是 |

**等价类划分**：目标文件层级 ∈ {根文件, 嵌套文件} → 代表值 = 嵌套 `docs/sub/y.md`

**Given**：tree = `docs/sub/{y.md}` + 根文件 `old.md`；`dataTransfer` stub
**When**：dragStart `kb-node-old.md` → dragOver + drop `kb-node-docs/sub/y.md`
**Then**：`onMoveNode` 调用参数 = `("old.md", "docs/sub")`（**落到 y.md 的父目录，而非 docs/sub/y.md 自身**）

---

#### 用例27：树内拖拽 drop 到树容器空白 → onMoveNode(src, "")

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-17 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：容器 `onDrop` → `handleDrop("")` |
| Oracle | golden value |
| Mock | 是 |

**Given**：tree 含 `docs/a.md`；`dataTransfer` stub
**When**：dragStart `kb-node-docs/a.md` → dragOver + drop `getByTestId("kb-tree-scroll")`
**Then**：`onMoveNode` 调用参数 = `("docs/a.md", "")`（移到根）

---

#### 用例28：拖拽文件夹到自身 → 不触发 onMoveNode

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-17 [P1]（`src === targetDir` 短路） |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`if (src === targetDir) return` |
| Oracle | golden value（回调未调用） |
| Mock | 是 |

**Given**：tree 含文件夹 `docs`（已展开）
**When**：dragStart `kb-node-docs` → dragOver `kb-node-docs` → drop `kb-node-docs`
**Then**：`onMoveNode` **未被调用**

---

#### 用例29：上传反馈渲染 uploaded / rejected

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-18 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`uploadFeedback` 非空且含条目 |
| Oracle | golden value（源码文案模板） |
| Mock | 是 |

**Given**：`uploadFeedback = {uploaded:[{name:"a.md",suffix:".md"}], rejected:[{name:"b.exe",reason:"不支持的类型"}]}`
**Then**：
- 文本 `"✓ 已上传 a.md"` 可见
- 文本 `"⚠ 已跳过 b.exe（不支持的类型）"` 可见

---

### E 组：KBContextMenu

---

#### 用例30：文件节点菜单含「预览」，且集合完整

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-19 [P1] + R6 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`!node.is_dir` 真 |
| Oracle | golden value（源码 items 顺序） |
| Mock | 否（真实渲染 + i18n） |

**Given**：`node = {path:"a.md", name:"a.md", is_dir:false}`；`render(<KBContextMenu x={10} y={10} node={node} onAction={vi.fn()} onClose={vi.fn()}/>)`
**Then**：菜单项文本序列 = `["预览","新建文件夹","上传到此处","重命名","删除"]`；无 `"移到根目录"`（根节点）

---

#### 用例31：文件夹节点菜单不含「预览」

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-19 [P1] + R6 菜单错配 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`!node.is_dir` 假 |
| Oracle | golden value |
| Mock | 否 |

**Given**：`node = {path:"docs", name:"docs", is_dir:true}`
**Then**：`queryByText("预览")` = `null`；`onAction` 项 = `["新建文件夹","上传到此处","重命名","删除"]`

---

#### 用例32：「移到根目录」随 path 是否含 "/" 显隐

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-19 [P1] + R6 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`hasParent = node.path.includes("/")` 真/假 |
| Oracle | golden value |
| Mock | 否 |

**等价类划分**：path ∈ {根（无 `/`）, 嵌套（含 `/`）} → 代表值 = `"docs"`（文件夹根）、`"docs/a.md"`（嵌套文件）

**When/Then**：
- 嵌套文件 `docs/a.md`：`getByText("移到根目录")` 存在
- 根文件夹 `docs`：`queryByText("移到根目录")` = `null`

---

#### 用例33：删除项为 danger

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-19 [P1] + R6 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`it.danger` 真（仅 delete） |
| Oracle | golden value（源码 `color:"var(--danger)"`） |
| Mock | 否 |

**Given**：文件节点菜单
**Then**：
- `getByText("删除").closest('[role="menuitem"]')` 的 `style.color` = `"var(--danger)"`
- `getByText("重命名").closest('[role="menuitem"]')` 的 `style.color` = `"var(--text-primary)"`（对照组）

---

#### 用例34：点击菜单项 → onAction(action) + onClose 各一次

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-20 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：项 `onClick` |
| Oracle | golden value |
| Mock | 否 |

**Given**：文件节点；`onAction`/`onClose` = `vi.fn()`
**When**：`fireEvent.click(getByText("重命名"))`
**Then**：`onAction` 调用 1 次且参数 = `"rename"`；`onClose` 调用 1 次

---

#### 用例35：Esc / 点击外部 → onClose

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-20 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：document `keydown(Escape)` / `mousedown`（outside） |
| Oracle | golden value |
| Mock | 否 |

**Given**：菜单渲染于 `document.body` 同级（`render` 容器内）
**When/Then**：
- `fireEvent.keyDown(document, {key:"Escape"})` → `onClose` 调用 1 次
- `fireEvent.mouseDown(document.body)` → `onClose` 再调用 1 次

---

### F 组：ConfirmDialog

---

#### 用例36：open=false → 不渲染

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-21 [P1] + R7 契约 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`if (!open) return null` |
| Oracle | golden value |
| Mock | 否 |

**Given**：`render(<ConfirmDialog open={false} title="删除" message="x" onConfirm={cf} onCancel={cc}/>)`
**Then**：`queryByTestId("kb-confirm-dialog")` = `null`；`queryByRole("dialog")` = `null`；`cf`/`cc` 未调用

---

#### 用例37：open=true 点确认 → onConfirm；danger → 确认按钮 btn-danger

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-21 [P1] + R7 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：确认按钮 `onClick`；`danger` 类名 |
| Oracle | golden value |
| Mock | 否 |

**Given**：`open={true} danger={true} confirmLabel="确认删除"`
**When**：`fireEvent.click(getByRole("button",{name:"确认删除"}))`
**Then**：
- `onConfirm` 调用 1 次；`onCancel` 未调用
- 确认按钮 `className` 含 `"btn-danger"`；`danger={false}` 时为 `"btn-primary"`（反例对照）

---

#### 用例38：点取消 → onCancel，不触发 onConfirm

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-21 [P1] + R7 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：取消按钮 `onClick` |
| Oracle | golden value |
| Mock | 否 |

**Given**：`open={true} cancelLabel="取消"`
**When**：`fireEvent.click(getByRole("button",{name:"取消"}))`
**Then**：`onCancel` 调用 1 次；`onConfirm` 未调用

---

#### 用例39：点击遮罩关闭，点击内容区不关闭

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-21 [P1] + R7 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：遮罩 `onClick` vs 内容区 `stopPropagation` |
| Oracle | golden value |
| Mock | 否 |

**Given**：`open={true}`
**When/Then**：
- `fireEvent.click(getByRole("dialog"))`（遮罩层）→ `onCancel` 1 次
- 重置 spy，`fireEvent.click(getByTestId("kb-confirm-dialog"))`（内容区，含 stopPropagation）→ `onCancel` 未调用

---

### G 组：KBDropZone.resolveDropDirAt

> 统一：构造真实 DOM（`document.body` 追加容器），`vi.spyOn(document,"elementFromPoint")` 钉死命中元素。

---

#### 用例40：命中文件夹行 → 返回其 path

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-22 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：`el.closest("[data-drop-dir]")` 命中 |
| Oracle | golden value |
| Mock | 是 — stub `document.elementFromPoint` |

**Given**：`<div data-drop-dir="docs"><span data-testid="leaf"/></div>` 挂到 body；`elementFromPoint` → `leaf`
**When**：`resolveDropDirAt(100, 100)`
**Then**：返回值 = `"docs"`

---

#### 用例41：命中文件行 → 返回其父目录 【P0 身份口径】

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-22 [P1] + R3/R8 父目录 [P0] |
| 测试层级 | unit |
| 覆盖准则 | 分支：同上（属性值即父目录） |
| Oracle | golden value（文件行的 `data-drop-dir` 由 KBFileTree 写为其父目录） |
| Mock | 是 |

**Given**：`<div data-drop-dir="docs/sub"><span data-testid="fileleaf"/></div>`；`elementFromPoint` → `fileleaf`
**When/Then**：`resolveDropDirAt(1,1)` = `"docs/sub"`（**证明文件行落点为父目录**）

---

#### 用例42：命中树容器（data-drop-dir=""）→ 返回 ""

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-22 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：`getAttribute` 返回空串（`?? null` 不误判） |
| Oracle | golden value |
| Mock | 是 |

**Given**：`<div data-drop-dir=""><span data-testid="rootleaf"/></div>`；`elementFromPoint` → `rootleaf`
**When/Then**：`resolveDropDirAt(1,1)` = `""`（**空串而非 null**）

---

#### 用例43：无命中 / 元素无祖先 → null

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-22 [P1] + R8 |
| 测试层级 | unit |
| 覆盖准则 | 分支：`if (!el) return null`；`if (!holder) return null` |
| Oracle | golden value |
| Mock | 是 |

**When/Then**：
- `elementFromPoint` → `null` → 返回 `null`
- `elementFromPoint` → 游离 `<div data-testid="bare"/>`（无 `[data-drop-dir]` 祖先）→ 返回 `null`

---

### H 组：kbStore

> 每例 `beforeEach`：`useKbStore.getState().reset()`。

---

#### 用例44：selectedPath 初始 null / set 生效 / 清空

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-23 [P1] |
| 测试层级 | unit |
| 覆盖准则 | N/A（setter） |
| Oracle | golden value |
| Mock | 否（真实 store） |

**When/Then**：
- 初始：`getState().selectedPath === null`
- `setSelectedPath("docs/a.md")` → `selectedPath === "docs/a.md"`
- `setSelectedPath(null)` → `selectedPath === null`

---

#### 用例45：rightTab 初始 "preview" / set 生效

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-24 [P1] |
| 测试层级 | unit |
| 覆盖准则 | N/A（setter） |
| Oracle | golden value |
| Mock | 否 |

**When/Then**：初始 `rightTab === "preview"`；`setRightTab("search")` → `"search"`；`setRightTab("settings")` → `"settings"`

---

#### 用例46：reset 清空 selectedPath / rightTab 回 preview

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-23/inv-24 [P1] + R9 |
| 测试层级 | unit |
| 覆盖准则 | N/A |
| Oracle | golden value |
| Mock | 否 |

**Given**：`setState({selectedPath:"x/y.md", rightTab:"search"})`
**When**：`reset()`
**Then**：`selectedPath === null`；`rightTab === "preview"`

---

#### 用例47：remove(name) 回收该库 tree/uploadFeedback/build，保留其他库

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-25 [P1] + R9 跨库残留 [P1] |
| 测试层级 | unit |
| 覆盖准则 | 分支：仅删目标 name 的派生条目 |
| Oracle | golden value |
| Mock | 否 |

**Given**：`setState({ kbs:[{name:"A"},{name:"B"}], treeByName:{A:[nodeA],B:[nodeB]}, uploadFeedbackByName:{A:fbA,B:fbB}, buildByName:{A:bA,B:bB} })`
**When**：`remove("A")`
**Then**：
- `treeByName.A` = `undefined`；`treeByName.B` = `[nodeB]`（B 保留）
- `uploadFeedbackByName.A` = `undefined`；`uploadFeedbackByName.B` = `fbB`
- `buildByName.A` = `undefined`；`buildByName.B` = `bB`
- `kbs.map(k=>k.name)` = `["B"]`

---

#### 用例48：setTree 写入并复位 treeLoading；setUploadFeedback 写入

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-25 邻接 [P1] |
| 测试层级 | unit |
| 覆盖准则 | N/A（setter） |
| Oracle | golden value |
| Mock | 否 |

**Given**：`setState({ treeLoading: true })`
**When**：`setTree("A", [nodeA])`；`setUploadFeedback("A", fbA)`
**Then**：
- `treeByName.A` = `[nodeA]`；`treeLoading === false`（顺带复位）
- `uploadFeedbackByName.A` = `fbA`

---

### I 组：KB_ERROR_MESSAGES

---

#### 用例49：8 个 KB 错误码 → 8 条互异非空中文文案 `[known-gap]`

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-26 [P1] + R10 错误码映射 [P1] |
| 测试层级 | unit |
| 覆盖准则 | N/A（常量表静态断言） |
| Oracle | golden value（映射表在源码中白纸黑字写死，可独立核对） |
| Mock | 否 — 纯常量表（**依赖 Known-Gap G1：需先导出**） |

**等价类划分**：错误码 = `kb_not_found / kb_busy / invalid_path / path_not_found / invalid_name / name_conflict / invalid_target / io_error`（8 个，全量）

**Given**：`import { KB_ERROR_MESSAGES } from "..."`（**当前不可导入，见 G1**）
**Then**：
- `Object.keys(KB_ERROR_MESSAGES)` 长度 = `8`，集合 = 上述 8 码
- 每条值为非空字符串且两两互异（防串值）
- golden：`kb_not_found→"知识库不存在"`、`kb_busy→"索引构建中，请稍后"`、`invalid_path→"非法路径"`、`path_not_found→"目标不存在，已刷新"`、`invalid_name→"名称不合法"`、`name_conflict→"同级已存在同名项"`、`invalid_target→"不能移动到自身子目录"`、`io_error→"文件操作失败"`

---

### J 组：KnowledgeBaseEditorPage（component(fake)，mock useBackend + dialog + router）

> 统一 Given：`MemoryRouter initialEntries={["/knowledge/MyKB/edit"]}` + `Route path="/knowledge/:name/edit"`；`useBackend` 14 个方法均为 `vi.fn()`；`kbStore.setState({ detail:{name:"MyKB",status:"ready",config:{documents_dir:"/kb/MyKB",...},documents:[]}, treeByName:{MyKB:<树>} })`；`i18n` zh-CN。

---

#### 用例50：右键删除文档 → 二次确认 → deleteKbDocument(kb, path)

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-27 [P1] + R7 契约 [P1] |
| 测试层级 | component(fake)（跨 store + IPC 边界，IPC 为 stub） |
| 覆盖准则 | 分支：`action==="delete"` → `setConfirmNode` → `doDelete` |
| Oracle | golden value（IPC 入参） |
| Mock | 是 — useBackend/dialog/i18n 真实/路由真实 |

**Given**：tree 含根文件 `a.md`
**When**：`fireEvent.contextMenu(getByTestId("kb-node-a.md"),{clientX:10,clientY:10})` → `fireEvent.click(getByText("删除"))` → `fireEvent.click(getByRole("button",{name:"确认删除"}))`
**Then**：
- 中间态：`getByRole("dialog")` 出现（二次确认已弹）
- 副作用：`deleteKbDocument` 调用 1 次且参数 = `("MyKB","a.md")`；`deleteKbFolder` 未调用

---

#### 用例51：删除文件夹 → 确认文案含后代计数 + deleteKbFolder

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-27 [P1] + R7 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`node.is_dir` 真 + `countDescendants > 0` |
| Oracle | golden value（文案模板 + IPC 入参） |
| Mock | 是 |

**Given**：tree 含文件夹 `docs`（含 2 个文件后代）
**When**：右键 `kb-node-docs` → 点「删除」→ 确认
**Then**：
- 对话框文本含 `"确定删除「docs」？其中含 2 个文档，"` 与 `"将从库内副本移除并触发索引更新。"`
- `deleteKbFolder` 调用 1 次且参数 = `("MyKB","docs")`

---

#### 用例52：取消删除 → 不调用 deleteKb*

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-27 [P1] + R7 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`onCancel` → `setConfirmNode(null)` |
| Oracle | golden value |
| Mock | 是 |

**When**：右键 `a.md` → 点「删除」→ 点「取消」
**Then**：`deleteKbDocument`/`deleteKbFolder` 均未调用；`queryByRole("dialog")` = `null`

---

#### 用例53：点击文件行 → selectedPath + rightTab=preview + 预览区显示

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-27 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`handleSelectNode` 且 `!node.is_dir` |
| Oracle | golden value（store 状态） |
| Mock | 是 |

**When**：`fireEvent.click(getByTestId("kb-node-a.md"))`
**Then**：
- `useKbStore.getState().selectedPath === "a.md"`
- `useKbStore.getState().rightTab === "preview"`
- 预览区渲染该文档（非空态）

---

#### 用例54：新建文件夹 → 弹窗默认名「新建文件夹」→ Enter 提交 createKbFolder

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-27 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`submitFolder` 有效提交 |
| Oracle | golden value（IPC 入参） |
| Mock | 是 |

**Given**：`selectedPath = null`（未选中 → `targetDirOf(null) = ""`）
**When**：点顶部 `"🗂 新建文件夹"` → 输入框默认值校验 → 改为 `"新目录"` → `fireEvent.keyDown(input,{key:"Enter"})`
**Then**：
- 弹窗输入框 `defaultValue/value` = `"新建文件夹"`
- `createKbFolder` 调用 1 次且参数 = `("MyKB", "", "新目录")`

---

#### 用例55：新建文件夹空名提交 → 不调用 createKbFolder

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-27 [P1] + R5 脏数据邻接 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`nm` falsy → 短路 |
| Oracle | golden value |
| Mock | 是 |

**When**：打开新建文件夹 → 清空输入 → `Enter`
**Then**：`createKbFolder` 未被调用；弹窗关闭

---

#### 用例56：右键「移到根目录」→ moveKbDocument(kb, path, "")

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-27 [P1] + R3 多层路径 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`action==="move-to-root"` |
| Oracle | golden value |
| Mock | 是 |

**Given**：tree 含嵌套文件 `docs/a.md`
**When**：右键 `kb-node-docs/a.md` → 点「移到根目录」
**Then**：`moveKbDocument` 调用 1 次且参数 = `("MyKB","docs/a.md","")`

---

#### 用例57：右键「上传到此处」→ targetDir 随 文件夹/文件 正确推导 【P0 身份口径】

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-27 [P1] + R1/R3/R8 目标目录 [P0] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`targetDirOf` 的 `is_dir` 真/假（文件夹用自身 path，文件用父目录） |
| Oracle | golden value（`open` 选择结果 + IPC 入参） |
| Mock | 是 — `@tauri-apps/plugin-dialog` `open` mock resolve `["/tmp/x.pdf"]` |

**等价类划分**：右键目标 ∈ {文件夹, 嵌套文件} → 代表值 = `docs`、`docs/a.md`

**When/Then**：
- 右键文件夹 `docs` → 点「上传到此处」→ `await waitFor`：`uploadKbDocument` 调用参数 = `("MyKB", ["/tmp/x.pdf"], "docs")`
- 右键文件 `docs/a.md` → 点「上传到此处」→ `uploadKbDocument` 参数 = `("MyKB", ["/tmp/x.pdf"], "docs")`（**父目录，非文件 path**）

---

#### 用例58：selectedPath 指向文件夹 → 预览 doc 为 null（空态）

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-28 [P1] + R3 |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`selectedNode && !selectedNode.is_dir` 假 |
| Oracle | golden value（空态文案） |
| Mock | 是 |

**When**：设 `selectedPath = "docs"`（文件夹）→ 切 `rightTab="preview"`
**Then**：预览区显示空态 `"从左侧文件树选择一个文档进行预览"`（`previewDoc === null`，不触发任何读盘）

---

#### 用例59：右键「重命名」→ renameRequest 注入 → 树进入重命名态

| 属性 | 内容 |
|------|------|
| 关联风险/不变式 | inv-27 [P1] |
| 测试层级 | component(fake) |
| 覆盖准则 | 分支：`action==="rename"` → `setRenameRequest` → KBFileTree effect |
| Oracle | golden value（重命名输入框出现） |
| Mock | 是 + `Date.now` stub（token 固定） |

**Given**：`Date.now` 固定为 `1700000000000`
**When**：右键 `a.md` → 点「重命名」
**Then**：`await waitFor(()=>getByTestId("kb-rename-input-a.md"))` 存在（树行进入内联重命名输入态）

---

## 7. Known-Gap 清单

| 编号 | 用例 | 期望行为（设计） | 实际现状 | 差距原因 / 处理 |
|------|------|-----------------|---------|----------------|
| G1 | 49 | `KB_ERROR_MESSAGES` 可作为模块常量被静态断言 | `BackendProvider.tsx:139` 定义为**模块私有 `const`（未 export）**，无法 `import` | 建议将 `KB_ERROR_MESSAGES` 加 `export`；在此之前该用例标 `test.fails`/`skip`，并在实现侧单列"可测试性需求"。**不迁就实现改设计**——映射表语义按期望写 |
| G2 | 25/26 | 拖拽同目录应被后端幂等吸收 | 文件行拖到自身所在目录（`src !== targetDir`，因文件 target 是父目录）会给后端发"移到当前目录"；前端仅拦截 `src === targetDir`（文件夹自投） | 影响：可能触发后端 `invalid_target` toast 噪声。**标记为观察项**（非测试失败），待确认后端是否幂等；本设计不新增失败用例，仅在第 26 例旁注 |

> 若实现方确认「G2 现状即正确行为（后端幂等）」→ 不新增用例；若确认应前端拦截同目录移动 → 需补 1 条用例（`onMoveNode` 不被调用），届时更新本节与覆盖矩阵。

---

## 8. 覆盖检查清单（豁免规则核对）

| 覆盖类别 | treeUtils / previewMode / textRendererKind / resolveDropDirAt | KBPreviewPanel | KBFileTree / Menu / Dialog | kbStore | EditorPage |
|---------|:--:|:--:|:--:|:--:|:--:|
| Happy path | ✅ 1/9/10/40 | ✅ 12/16 | ✅ 20/30 | ✅ 44 | ✅ 50 |
| 边界值 | ✅ 2/6/13 | ✅ 13 | ✅ 19 | ✅ 46 | ✅ 55 |
| 异常路径 | 豁免（无外部依赖） | ✅ 17 | ✅ 23/43 | 豁免（纯状态） | ✅ 52 |
| 副作用 | 豁免（无副作用） | ✅ 12/15（join/read 调用性） | ✅ 22/25/26/34 | ✅ 47/48 | ✅ 50/56/57 |
| 回滚/清理 | 豁免 | ✅ 18（切换清空） | ✅ 21（折叠复位） | ✅ 46/47 | ✅ 52（取消无副作用） |
| 故障注入 | 豁免（无外部依赖） | ✅ 17（readTextFile reject） | 豁免（无外部 I/O） | 豁免 | 豁免（IPC 为 stub，见下） |

> **EditorPage 故障注入豁免理由**：页面层不直接持有外部 I/O，IPC 失败反馈由 `BackendProvider` 的 `KB_ERROR_MESSAGES`（用例49）+ 后端错误码负责，页面层只负责"发对消息"（用例50–57 已验入参）。后端错误码 → toast 的端到端链路（含驱动 `ERROR` 消息）属 integration，需真实 `BackendProvider` + 传输层 mock，**不在本次前端改造范围，单列待办**。

---

## 9. 待确认（推断项标注）

- **[推断，请确认]** 用例1 期望顺序依赖 `Intl.Collator("zh")` 的 ASCII 大小写排序（`Alpha < beta`、`a.md < b.md`）。若 CI 环境 Node ICU 为 small-icu，`localeCompare(...,"zh")` 可能退化为字节序 → 建议 test-coder 用 `expect(out.map(n=>n.name)).toEqual([...])` 并确认 CI Node 全 ICU；必要时改用不依赖中文 collation 的纯 ASCII 用例名。
- **[推断，请确认]** `KBPreviewPanel` 空态/降级/过大文案为源码硬编码字面量（非 i18n），故无需 i18n 依赖即可断言；header 的扩展名/大小文案依赖 `formatSize`（`0 B / KB / MB`），本设计未单列用例（非本次改造核心），如需要可补 1 例。
- **[推断，请确认]** 用例50–59 假定 `useBackend` 暴露的 14 个方法名与 `KnowledgeBaseEditorPage` 解构一致（已核对源码 76 行）；若后续新增解构键需同步补齐 stub。
