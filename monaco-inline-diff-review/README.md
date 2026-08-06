# monaco-inline-diff-review

> Inline code diff review for Monaco Editor — per-hunk accept/reject, just like VS Code's built-in diff review.

为 Monaco Editor 提供内联 diff 审阅能力：AI 建议的改动按 hunk（变更块）展示，支持逐个 **Apply / Reject**，也支持 **Apply All / Reject All**，交互体验对齐 VS Code 内置的 diff review。

## 特性

- **逐 hunk 审阅**：每个变更块（add / del / modify）附带 Apply / Reject 按钮，删除行以 ViewZone 内联展示，新增行用装饰器高亮
- **全局操作栏**：存在未处理 hunk 时，底部浮动栏提供 Apply All / Reject All
- **稳定身份标识**：hunk 通过 `contentKey` 锚定在不可变的 original 行区间上，Reject 其他 hunk 导致 model 变化后 key 不漂移，支持跨页签/跨文件持久化已 Apply 状态
- **非受控设计**：`defaultValue` + 命令式 model 操作，Reject 修改 model 后不会被 React 回滚
- **三层架构**：纯算法引擎（零依赖，Node.js 可用）/ React 组件 / 多文件状态 Hook，按需取用
- **完整类型**：TypeScript 编写，ESM + CJS 双产物，类型定义随包发布

## 安装

```bash
npm install monaco-inline-diff-review
# 或
pnpm add monaco-inline-diff-review
```

peer dependencies（需自行安装）：

| 包 | 版本 |
|------|------|
| `react` / `react-dom` | ^18.0.0 |
| `monaco-editor` | ^0.52.0 |
| `@monaco-editor/react` | ^4.7.0 |

引入样式（必须，否则 diff 高亮和按钮无样式）：

```ts
import "monaco-inline-diff-review/styles";
```

## 快速开始

### 方案一：完整方案 —— `CodeRenderer` + `useDiffSuggestions`

适合多文件 AI 建议场景（如 Chat 驱动代码修改）。`CodeRenderer` 根据 props 自动切换模式：传入 `original` 且 `readOnly` 时进入 **suggestion 模式**（inline diff 审阅），否则为 **edit 模式**（可编辑 Monaco + 未保存修改标记）。

```tsx
import { CodeRenderer, useDiffSuggestions } from "monaco-inline-diff-review";
import "monaco-inline-diff-review/styles";

function FilePanel() {
  const { suggestions, showSuggestion, updateSuggestion, markApplied, clearSuggestion } =
    useDiffSuggestions();

  // 收到 AI 建议时
  const onAiSuggestion = (path: string, original: string, suggested: string) =>
    showSuggestion(path, original, suggested);

  const filePath = "/src/app.py";
  const s = suggestions[filePath];

  return s ? (
    <CodeRenderer
      fileId={filePath}
      language="python"
      readOnly
      original={s.original}
      content={s.suggested}
      initialAppliedKeys={s.appliedContentKeys}
      onPartialSave={(content, hunkKey) => {
        updateSuggestion(filePath, content);   // 同步 Reject 后的内容
        if (hunkKey) markApplied(filePath, hunkKey); // 记录已 Apply 的 hunk
      }}
      onAllResolved={(final) => {
        saveFile(filePath, final);             // 全部处理完毕，落盘
        clearSuggestion(filePath);
      }}
    />
  ) : (
    <CodeRenderer fileId={filePath} language="python" content={fileContent}
                  onChange={(v) => setFileContent(v)} />
  );
}
```

### 方案二：只要 diff 组件 —— `InlineDiffEditor`

```tsx
import { InlineDiffEditor } from "monaco-inline-diff-review";
import "monaco-inline-diff-review/styles";

<InlineDiffEditor
  original={originalCode}
  current={aiSuggestedCode}
  language="typescript"
  onPartialSave={(content, hunkKey) => console.log("partial", hunkKey)}
  onAllResolved={(final) => console.log("resolved", final)}
/>
```

### 方案三：纯算法 —— `engine`（Node.js / 浏览器通用，零依赖）

```ts
import { computeDiff, groupHunks, hashStr } from "monaco-inline-diff-review/engine";

const entries = computeDiff("a\nb\nc", "a\nx\nc");
// [{ kind:"ctx", text:"a" }, { kind:"del", text:"b" }, { kind:"add", text:"x" }, { kind:"ctx", text:"c" }]

const hunks = groupHunks(entries);
// [{ type:"modify", delLines:["b"], addLines:["x"], contentKey:"modify#1-2#..#..", ... }]
```

## API

### `<InlineDiffEditor />`

| Prop | 类型 | 说明 |
|------|------|------|
| `original` | `string` | 原始文本（不可变锚点） |
| `current` | `string` | 当前 / AI 建议文本 |
| `language` | `string` | Monaco 语言标识符（如 `"python"`、`"typescript"`） |
| `onAllResolved?` | `(savedContent: string) => void` | 所有 hunk 处理完毕后回调，携带最终文本 |
| `onPartialSave?` | `(content: string, hunkKey: string) => void` | 单个 hunk Apply/Reject 后即时回调；Reject 时 `hunkKey` 为 `""` |
| `initialAppliedKeys?` | `string[]` | 已 Apply 的 hunk `contentKey` 列表，用于恢复持久化状态 |

### `<CodeRenderer />`

| Prop | 类型 | 说明 |
|------|------|------|
| `content` | `string` | 当前内容（edit 模式）或 AI 建议内容（suggestion 模式） |
| `language` | `string` | Monaco 语言标识符 |
| `original?` | `string` | 原始内容；存在且 `readOnly` 时进入 suggestion 模式 |
| `readOnly?` | `boolean` | 是否只读 |
| `fileId?` | `string` | 文件标识，建议作为 React `key` 使用，防止跨文件状态残留 |
| `onChange?` | `(value: string) => void` | 编辑回调（edit 模式） |
| `onAllResolved?` / `onPartialSave?` / `initialAppliedKeys?` | 同 `InlineDiffEditor` | suggestion 模式回调 |

### `useDiffSuggestions()`

多文件 AI 建议状态管理，与上述回调无缝对接。

| 返回 | 说明 |
|------|------|
| `suggestions` | `Record<path, { original, suggested, appliedContentKeys }>` |
| `showSuggestion(path, original, suggested)` | 展示一条建议（覆盖同路径已有建议） |
| `updateSuggestion(path, suggested)` | 更新建议内容（Reject 后同步，避免切文件丢状态） |
| `markApplied(path, contentKey)` | 标记已 Apply 的 hunk |
| `clearSuggestion(path)` | 清除一条建议 |

### `engine` 子路径导出

| 导出 | 说明 |
|------|------|
| `computeDiff(orig, cur)` | 行级 LCS diff，返回 `DiffEntry[]`（`ctx` / `del` / `add`） |
| `groupHunks(entries)` | 合并相邻 del↔add 为 modify，返回锚定 original 行区间的 `Hunk[]` |
| `hashStr(s)` | djb2 哈希，base-36 输出 |
| 类型 | `DiffEntry`、`Hunk`、`HunkType`、`Suggestion`、`InlineDiffEditorProps`、`CodeRendererProps` |

## Monaco 加载说明

组件基于 `@monaco-editor/react`，默认从 CDN 加载 Monaco。离线环境 / Electron / Tauri 应用需改为本地加载：

```ts
import { loader } from "@monaco-editor/react";
loader.config({ paths: { vs: "/vs" } }); // 把 monaco-editor/min/vs 拷到可访问路径
```

## Demo 与测试

```bash
pnpm install
pnpm demo        # 浏览器台架（tests/demo），默认 case=multi_modify
pnpm test        # vitest 单元 + 组件测试（tests/vitest.config.ts）
pnpm test:e2e    # Playwright 端到端测试（tests/playwright.config.ts，自动拉起 demo dev server）
pnpm build       # 产出 dist/（ES + CJS + .d.ts）
```

测试相关文件全部收敛在 `tests/` 目录下：单元/组件用例（`unit/`、`component/`）、e2e 用例（`e2e/`）、demo 台架（`demo/`）、测试配置（`vitest.config.ts`、`playwright.config.ts`、`vite.demo.config.ts`）与公共夹具（`fixtures/`、`setup.ts`）。

Demo 支持 URL 参数切换场景：`?case=greet`（TS 重构）、`?case=three_funcs`、`?case=renderer`（CodeRenderer 模式）等，完整场景见 `tests/demo/app.tsx` 的 `SCENARIOS`。

## 设计要点

- **contentKey 稳定性**：hunk 以 original 中的行区间 `[origStart, origEnd)` 为锚点，格式 `{type}#{origStart}-{origEnd}#{hashDel}#{hashAdd}`。锚点保证跨 model 变化稳定唯一；哈希段保证「同位置换了新内容」时作为新 hunk 重新出现。
- **全量重建**：每次状态变化后通过 `OverlayManager` 全量清除并重建 decoration / ViewZone / contentWidget，避免增量更新导致的 UI 残留。
- **行数保护**：edit 模式的未保存标记对超过 3000 行的文件自动跳过（LCS 复杂度 O(n·m)）。

## 许可证

MIT
