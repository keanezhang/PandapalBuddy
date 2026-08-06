# 数据流图 HTML 风格指南

> 本文件是数据流图唯一风格来源。生成 HTML 时直接引用本文件中的完整 CSS 模板，**不依赖任何产物 HTML 文件**。

---

## 一、设计原则

| 原则 | 说明 |
|------|------|
| **暗黑主题** | 背景 `#0f1117`，层次靠 `#161b22` / `#0d1117` 区分 |
| **单文件自洽** | 所有样式内联在 `<style>` 中，无外部依赖，可直接用浏览器打开 |
| **五元组核心** | 卡片=文件路径+类名.方法名+解释+格式+归属注释 · 箭头=协议通道 · 所有箭头流动白点动画 |
| **全景自由布局** | 主链垂直+扇出扇入+子步骤横向，上下左右不局限 |
| **颜色有语义** | 紫=入口，蓝=处理/匹配，橙=执行/桥接，绿=出口/UI，红=缺口 |
| **动效克制** | 箭头白点 `flowVert` 动画 + 节点 `fadeIn` 入场 + `:hover` 偏移 |

---

## 二、CSS 变量系统

```css
:root {
  --bg: #0f1117; --s: #161b22; --s2: #0d1117; --brd: #30363d;
  --bl: #58a6ff; --pu: #bc8cff; --or: #f0883e;
  --gr: #7ee787; --re: #f85149; --ye: #e3b341;
  --txt: #e1e4e8; --t2: #8b949e;
}
```

---

## 三、核心组件库

### 3.1 全景图容器（Graph）

```html
<div class="graph">
  <!-- g-row → g-node → g-arrow → ... -->
</div>
```

`graph` 是垂直 flexbox，`g-row` 控制每行布局（单列 `single` / 多列并行 `branch`）。

### 3.2 节点卡片（g-node）

全景图基本单元。五元组中「文件路径」「类名.方法名」「解释」「格式」「归属注释」落在这里。

```html
<div class="g-node purple">
  <div class="g-head">
    <span class="dot"></span>
    <span class="name">类名.方法名()</span>
    <span class="role">相对文件路径:行号</span>
  </div>
  <div class="g-body">
    <p class="explain">
      <em>做什么：</em>解释这步干了什么。<br/>
      <em>为什么：</em>解释为什么需要这一步。<br/>
      <em>什么意思：</em>解释数据怎么变、对下游的影响。
    </p>
    <div class="dl">数据摘要</div>
    <pre class="fd"><span class="fc">// 文件路径 · 类名.方法名() → 入参/出参</span>
<span class="fk">"key"</span>: <span class="fs">"value"</span>,
<span class="fk">"count"</span>: <span class="fn">42</span></pre>
  </div>
</div>
```

**g-head 字段约定**：
- `.name` = `类名.方法名()`（带括号），如 `SkillRegistry.search_skills()`
- `.role` = 相对项目根目录的 `文件路径:行号`，如 `pandaren/skill/registry.py:362`

**g-body 结构约定**：
- 先放 `<p class="explain">` 解释段落——像代码注释一样，说清楚做什么+为什么+什么意思
- 再放 `<div class="dl">` 数据摘要标签
- 最后放 `<pre class="fd">` 代码块，首行必须 `// 文件路径 · 类名.方法名() → 入参/出参` 注明归属

**颜色变体**（class 名 + 颜色语义）：

| class | 颜色 | 语义 |
|-------|------|------|
| `purple` | 紫 `#bc8cff` | 入口 / 触发 (LLM Agent) |
| `blue` | 蓝 `#58a6ff` | 处理 / 匹配 (SkillRegistry) |
| `orange` | 橙 `#f0883e` | 执行 / 桥接 (SkillToolBridge) |
| `green` | 绿 `#7ee787` | 出口 / UI (Frontend) |
| `red` | 红 `#f85149` | 缺口 / 缺失 |

### 3.3 布局行（g-row）

```html
<!-- 单列居中 -->
<div class="g-row single">
  <div class="g-node purple">...</div>
</div>

<!-- 并行分支 -->
<div class="g-row branch">
  <div class="g-node safety">分支 A</div>
  <div class="g-node skill">分支 B</div>
</div>
```

`single` 让节点居中（`max-width` 控制宽度），`branch` 让多节点等宽并排。

### 3.4 协议箭头（g-arrow）

三元组中「什么协议」落在这里。垂直箭头 + 协议标签 + 流动白点。

```html
<div class="g-arrow">
  <div class="line"></div>
  <div class="g-proto proto-invoke">Agent Tool Invocation</div>
</div>
```

**协议标签颜色变体**：
- `proto-invoke` — 入口调用（紫色）
- `proto-method` — 方法链（蓝色）
- `proto-exec` — 执行器（橙色）
- `proto-ipc` — IPC 通道（橙色）
- `proto-tauri` — Tauri Event（蓝色）
- `proto-zustand` — 状态管理（灰色）
- `proto-gap` — 缺口（红色）

**缺口箭头**：加 `.gap` 类，线变红：

```html
<div class="g-arrow gap">
  <div class="line"></div>
  <div class="g-proto proto-gap">🔴 缺失</div>
</div>
```

### 3.5 系统概览缩略图（flow-chain）**【必须】**

全景图顶部必须有一行 flow-chain 缩略图，展示关键节点串联，给人宏观全貌。

```html
<div class="overview">
  <div class="overview-title">系统概览</div>
  <div class="flow-chain">
    <div class="flat-node flat-llm"><div class="node-icon">🤖</div><div class="node-title">LLM Agent</div><div class="node-desc">search_skills()</div></div>
    <div class="chain-link"><div class="chain-line"></div><span class="chain-label">Agent Tool Invocation</span></div>
    <div class="flat-node flat-agent"><div class="node-icon">🧠</div><div class="node-title">SkillRegistry</div><div class="node-desc">match·gate·activate</div></div>
    <!-- ... -->
  </div>
</div>
```

### 3.6 Fan-Out 分叉 / Fan-In 汇聚

并行分支的连接器。

```html
<!-- Fan-Out -->
<div class="fan-zone">
  <div class="fan-out" style="min-height:70px">
    <div class="stem"></div>
    <div class="split"></div>
    <div class="branch-left" style="left:26%;"></div>
    <div class="branch-right" style="left:74%;"></div>
    <div class="proto-left" style="left:20%">pre_process</div>
    <div class="proto-right" style="left:70%">search_skills</div>
  </div>
</div>

<!-- Fan-In -->
<div class="fan-zone">
  <div class="fan-in" style="min-height:64px">
    <div class="arm-left" style="left:26%;"></div>
    <div class="arm-right" style="left:74%;"></div>
    <div class="merge-dot" style="left:50%;"></div>
    <div class="stem-down" style="left:50%;"></div>
    <div class="proto-merge" style="left:47%">合并结果</div>
  </div>
</div>
```

`left` 值需根据实际列宽计算（分支数为 N，则第 i 个分支的 left ≈ `100% / N * (i - 0.5)`）。

### 3.7 模块内部子步骤链（sub-chain）

节点内部的数据转换步骤，横向排列。每个 sub-node 同样需要解释段落。

```html
<div class="sub-chain">
  <div class="sub-node">
    <div class="dl">① 步骤名 · 文件路径:行号</div>
    <p class="sub-explain">
      <em>做什么：</em>简短解释。<br/>
      <em>为什么：</em>简短解释。<br/>
      <em>输出：</em>简述输出。
    </p>
    <pre class="fd" style="font-size:9px;line-height:1.6;padding:5px 8px;"><span class="fc">// 文件路径 · 类名.方法名() → 出参</span>
<span class="fk">"key"</span>: <span class="fs">"value"</span></pre>
  </div>
  <div class="sub-arrow"><div class="sub-arr-line"></div></div>
  <div class="sub-node">
    <div class="dl">② 步骤名 · 文件路径:行号</div>
    <p class="sub-explain">
      <em>做什么：</em>简短解释。<br/>
      <em>含义：</em>对后续的影响。
    </p>
    <pre class="fd" style="font-size:9px;line-height:1.6;padding:5px 8px;"><span class="fc">// 文件路径 · 类名.方法名() → 出参</span>
<span class="fk">"key"</span>: <span class="fs">"value"</span></pre>
  </div>
</div>
```

### 3.8 数据结构展示（fd · pre + 语法高亮）

```html
<pre class="fd"><span class="fk">"type"</span>: <span class="fs">"DELETE"</span>,
<span class="fk">"skill_name"</span>: <span class="fs">"my-skill"</span>,
<span class="fk">"count"</span>: <span class="fn">42</span>,
<span class="fk">"active"</span>: <span class="fb">true</span>,
<span class="fk">"error"</span>: <span class="fu">null</span>
<span class="fc">// 注释：数据在此步发生变形</span></pre>
```

**语法高亮 class**：

| class | 颜色 | 用途 |
|-------|------|------|
| `fk` | 蓝色 `#58a6ff` | 字段名 |
| `fs` | 黄色 `#e3b341` | 字符串值 |
| `fn` | 橙色 `#f0883e` | 数字 |
| `fb` | 紫色 `#bc8cff` | 布尔值 |
| `fu` | 灰色 `#8b949e` | null/None |
| `fc` | 灰色斜体 | 注释 |

### 3.9 数据标签（dl）

```html
<div class="dl">出站格式</div>
```

---

## 四、页面整体骨架

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>数据流链路名称</title>
  <style>/* 粘贴 §五 完整 CSS */</style>
</head>
<body>

<h1>标题</h1>
<p>概述</p>

<div class="graph">

  <!-- 节点 -->
  <div class="g-row single">
    <div class="g-node purple">
      <div class="g-head"><span class="dot"></span><span class="name">类名.方法名()</span><span class="role">路径/文件.py:行号</span></div>
      <div class="g-body">
        <p class="explain">
          <em>做什么：</em>解释这步干了什么。<br/>
          <em>为什么：</em>解释为什么需要这一步。<br/>
          <em>什么意思：</em>解释数据怎么变、对下游的影响。
        </p>
        <div class="dl">数据摘要</div>
        <pre class="fd"><span class="fc">// 文件路径 · 类名.方法名() → 入参/出参</span>
<span class="fk">"key"</span>: <span class="fs">"value"</span></pre>
      </div>
    </div>
  </div>

  <!-- 箭头 -->
  <div class="g-arrow">
    <div class="line"></div>
    <div class="g-proto proto-invoke">协议名</div>
  </div>

  <!-- 并行分支（需要时） -->
  <div class="fan-zone"><div class="fan-out">...</div></div>
  <div class="g-row branch">
    <div class="g-node safety">...</div>
    <div class="g-node skill">...</div>
  </div>
  <div class="fan-zone"><div class="fan-in">...</div></div>

  <!-- 汇聚后继续 -->
  <div class="g-arrow"><div class="line"></div><div class="g-proto proto-exec">协议名</div></div>

  <!-- 下一个节点 -->
  <div class="g-row single">
    <div class="g-node green">...</div>
  </div>

</div>

</body>
</html>
```

---

## 五、快速复用 checklist

- [ ] 复制完整 `<style>` 块（§六 data-flow 完整 CSS）
- [ ] 设置 `<title>` 和 h1 文字
- [ ] 按"链路概述 → 系统概览 → 全景图节点"顺序组织，必要时在节点间插入 Fan-Out/Fan-In
- [ ] 每个节点 = `g-node`（class 按颜色语义）+ `g-head`（`name="类名.方法名()"` `role="文件路径:行号"`）+ `g-body`（`<p class="explain">` 解释 + `dl` 摘要 + `fd` 代码块）
- [ ] 每两个节点间 = `g-arrow`（协议箭头 + 流动白点）
- [ ] 模块内部转换链用 `sub-chain` + `sub-node`（含 `sub-explain` 解释）+ `sub-arrow`
- [ ] 所有结构化数据用 `fd` + `fk/fs/fn/fb/fu/fc` 语法高亮，**首行必须标注归属注释** `// 文件路径 · 类名.方法名() → 入参/出参`
- [ ] 缺口节点用 `g-node red` + `g-arrow gap` + `proto-gap`，**必须包含**：解释（为什么缺）+ 当前状态（缺什么）+ 需新增的数据结构（fd 完整展示字段/类型/示例值）+ 修改位置（哪个模块哪个方法）+ 含义说明
- [ ] 所有箭头有流动白点动画，白点大小 2-4px
- [ ] 一张图一条端到端链路，可含并行分支

---

## 六、data-flow 完整样式 — 完整 `<style>` 模板

> 生成数据流 HTML 时，**直接将下方 `<style>` 块整体粘贴**到 `<head>` 中。

```css
/* ── data-flow 全景图 完整样式 ── */
* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #0f1117; color: #e1e4e8;
  min-height: 100vh; overflow-x: hidden; padding: 40px 20px 60px;
}

:root {
  --bg: #0f1117; --s: #161b22; --s2: #0d1117; --brd: #30363d;
  --bl: #58a6ff; --pu: #bc8cff; --or: #f0883e;
  --gr: #7ee787; --re: #f85149; --ye: #e3b341;
  --txt: #e1e4e8; --t2: #8b949e;
}

/* ── 全景图容器 ── */
.graph { display: flex; flex-direction: column; align-items: stretch; gap: 0; max-width: 1200px; margin: 0 auto; }

/* ── 行布局 ── */
.g-row { display: flex; gap: 0; align-items: stretch; }
.g-row.branch { gap: 24px; }
.g-row.single { justify-content: center; }

/* ── 节点卡片 ── */
.g-node {
  flex: 1; min-width: 0; border: 1px solid rgba(48,54,61,0.5); border-radius: 14px;
  background: rgba(22,27,34,0.5); overflow: hidden;
  animation: fadeIn 0.4s ease backwards;
}
.g-node:nth-child(1) { animation-delay: .05s; }
.g-node:nth-child(2) { animation-delay: .15s; }
.g-node:nth-child(3) { animation-delay: .25s; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

.g-node .g-head {
  display: flex; align-items: center; gap: 10px; padding: 12px 16px;
  border-bottom: 1px solid rgba(48,54,61,0.3);
}
.g-node .g-head .dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.g-node .g-head .name { font-size: 14px; font-weight: 700; }
.g-node .g-head .role { font-size: 10px; color: #8b949e; margin-left: auto; text-align: right; }
.g-node .g-body { padding: 12px 16px; }

/* ── 节点解释段落（explain · 像代码注释一样）── */
.g-node .g-body .explain { font-size: 11px; color: #8b949e; line-height: 1.7; margin-bottom: 10px; }
.g-node .g-body .explain em { color: #e3b341; font-style: normal; }

/* ── 子步骤解释段落（sub-explain）── */
.sub-node .sub-explain { font-size: 10px; color: #8b949e; line-height: 1.6; margin-bottom: 6px; }
.sub-node .sub-explain em { color: #e3b341; font-style: normal; }

/* 颜色变体 */
.g-node.purple  .g-head { background: rgba(188,140,255,0.04); border-color: rgba(188,140,255,0.2); }
.g-node.purple  .g-head .name { color: #bc8cff; }
.g-node.purple  .g-head .dot { background: #bc8cff; }
.g-node.blue    .g-head { background: rgba(88,166,255,0.04); border-color: rgba(88,166,255,0.2); }
.g-node.blue    .g-head .name { color: #58a6ff; }
.g-node.blue    .g-head .dot { background: #58a6ff; }
.g-node.orange  .g-head { background: rgba(240,136,62,0.04); border-color: rgba(240,136,62,0.2); }
.g-node.orange  .g-head .name { color: #f0883e; }
.g-node.orange  .g-head .dot { background: #f0883e; }
.g-node.green   .g-head { background: rgba(126,231,135,0.04); border-color: rgba(126,231,135,0.2); }
.g-node.green   .g-head .name { color: #7ee787; }
.g-node.green   .g-head .dot { background: #7ee787; }
.g-node.red { border-color: rgba(248,81,73,0.4); background: rgba(248,81,73,0.03); }
.g-node.red .g-head { background: rgba(248,81,73,0.06); border-color: rgba(248,81,73,0.3); }
.g-node.red .g-head .name { color: #f85149; }
.g-node.red .g-head .dot { background: #f85149; }

/* ── 垂直协议箭头（节点间）── */
.g-arrow {
  display: flex; flex-direction: column; align-items: center;
  width: 2px; min-height: 56px; margin: 0 auto; position: relative;
}
.g-arrow .line {
  width: 2px; flex: 1; min-height: 36px;
  background: linear-gradient(180deg, #bc8cff, #f0883e, #7ee787);
  position: relative; overflow: visible;
}
.g-arrow .line::after {
  content: ''; position: absolute; bottom: -6px; left: 50%; transform: translateX(-50%);
  border-left: 7px solid transparent; border-right: 7px solid transparent;
  border-top: 9px solid #7ee787;
}
.g-arrow .line::before {
  content: ''; position: absolute;
  left: -3px; width: 8px; height: 8px;
  background: #fff; border-radius: 50%;
  box-shadow: 0 0 6px 3px rgba(255,255,255,0.5);
  animation: flowVert 2.2s ease-in-out infinite;
}
@keyframes flowVert {
  0%   { top: 0; opacity: 1; }
  70%  { opacity: 1; }
  100% { top: calc(100% - 8px); opacity: 0; }
}

.g-proto {
  padding: 6px 14px; border-radius: 14px; font-size: 11px; font-weight: 600;
  text-align: center; margin-top: 8px;
}
.proto-invoke  { background: rgba(188,140,255,0.1);  color: #bc8cff; border: 1px solid rgba(188,140,255,0.2); }
.proto-method  { background: rgba(88,166,255,0.08);   color: #58a6ff; border: 1px solid rgba(88,166,255,0.2); }
.proto-exec    { background: rgba(240,136,62,0.08);   color: #f0883e; border: 1px solid rgba(240,136,62,0.2); }
.proto-ipc     { background: rgba(240,136,62,0.08);   color: #f0883e; border: 1px solid rgba(240,136,62,0.2); }
.proto-tauri   { background: rgba(58,166,255,0.08);   color: #58a6ff; border: 1px solid rgba(58,166,255,0.2); }
.proto-zustand { background: rgba(139,148,158,0.08);  color: #8b949e; border: 1px solid rgba(139,148,158,0.2); }
.proto-gap     { background: rgba(248,81,73,0.08);    color: #f85149; border: 1px solid rgba(248,81,73,0.3); }

/* gap 箭头 */
.g-arrow.gap .line { background: #f85149; }
.g-arrow.gap .line::after { border-top-color: #f85149; }
.g-arrow.gap .line::before { box-shadow: 0 0 6px 3px rgba(248,81,73,0.6); }

/* ── Fan-Out 分叉 ── */
.fan-zone { display: flex; justify-content: center; align-items: flex-start; }
.fan-out {
  display: flex; align-items: stretch; justify-content: center;
  width: 100%; min-height: 60px; position: relative;
}
.fan-out .stem {
  width: 2px; background: linear-gradient(180deg, #bc8cff, #8b949e88);
  position: absolute; top: 0; height: 28px; left: 50%; transform: translateX(-50%);
}
.fan-out .stem::before {
  content: ''; position: absolute;
  left: -3px; width: 8px; height: 8px;
  background: #fff; border-radius: 50%;
  box-shadow: 0 0 6px 3px rgba(255,255,255,0.5);
  animation: flowVert 2.2s ease-in-out infinite;
}
.fan-out .split {
  position: absolute; top: 28px; left: 50%; transform: translateX(-50%);
  width: 0; height: 0;
  border-left: 8px solid transparent; border-right: 8px solid transparent;
  border-top: 8px solid #8b949e;
}
.fan-out .branch-left {
  width: 2px; background: linear-gradient(180deg, #8b949e88, #f0883e);
  position: absolute; top: 35px; height: 18px;
}
.fan-out .branch-right {
  width: 2px; background: linear-gradient(180deg, #8b949e88, #58a6ff);
  position: absolute; top: 35px; height: 18px;
}
.fan-out .proto-left { position: absolute; top: 32px; font-size: 9px; font-weight: 600; color: #f0883e; white-space: nowrap; }
.fan-out .proto-right { position: absolute; top: 32px; font-size: 9px; font-weight: 600; color: #58a6ff; white-space: nowrap; }

/* ── Fan-In 汇聚 ── */
.fan-in {
  display: flex; align-items: stretch; justify-content: center;
  width: 100%; min-height: 56px; position: relative;
}
.fan-in .arm-left, .fan-in .arm-right {
  width: 2px; background: linear-gradient(180deg, #8b949e88, #7ee78788);
  position: absolute; top: 0; height: 36px;
}
.fan-in .merge-dot {
  position: absolute; top: 36px; left: 50%; transform: translateX(-50%);
  width: 8px; height: 8px; background: #7ee787; border-radius: 50%;
}
.fan-in .stem-down {
  width: 2px; background: #7ee787;
  position: absolute; top: 44px; left: 50%; transform: translateX(-50%); height: 14px;
}
.fan-in .stem-down::after {
  content: ''; position: absolute; bottom: -6px; left: 50%; transform: translateX(-50%);
  border-left: 6px solid transparent; border-right: 6px solid transparent;
  border-top: 7px solid #7ee787;
}
.fan-in .proto-merge { position: absolute; top: 28px; font-size: 9px; font-weight: 600; color: #7ee787; white-space: nowrap; }

/* ── 模块内部子步骤链 ── */
.sub-chain { display: flex; gap: 0; align-items: stretch; margin-top: 8px; }
.sub-node {
  flex: 1; border: 1px solid rgba(48,54,61,0.4); border-radius: 8px;
  background: rgba(13,17,23,0.5); padding: 10px 12px; min-width: 0;
}
.sub-arrow {
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0; width: 32px;
}
.sub-arr-line {
  width: 100%; height: 2px;
  background: linear-gradient(90deg, rgba(188,140,255,0.3), rgba(88,166,255,0.5));
  position: relative; overflow: visible;
}
.sub-arr-line::after {
  content: ''; position: absolute; right: -4px; top: 50%; transform: translateY(-50%);
  border-left: 5px solid #58a6ff;
  border-top: 3px solid transparent; border-bottom: 3px solid transparent;
}
.sub-arr-line::before {
  content: ''; position: absolute;
  top: -2px; width: 6px; height: 6px;
  background: #fff; border-radius: 50%;
  box-shadow: 0 0 4px 2px rgba(255,255,255,0.4);
  animation: flowHoriz 2s ease-in-out infinite;
}
@keyframes flowHoriz {
  0%   { left: 0; opacity: 1; }
  70%  { opacity: 1; }
  100% { left: calc(100% - 6px); opacity: 0; }
}

/* ── 数据结构展示（fd · pre + 语法高亮）── */
.fd {
  background: #0d1117; border: 1px solid rgba(48,54,61,0.5); border-radius: 8px;
  padding: 8px 12px; font-family: 'SF Mono', Consolas, monospace; font-size: 10px;
  line-height: 1.8; color: #7ee787; white-space: pre; margin: 0; overflow-x: auto;
}
.fk { color: #58a6ff; } .fs { color: #e3b341; } .fn { color: #f0883e; }
.fb { color: #bc8cff; } .fu { color: #8b949e; } .fc { color: #8b949e; font-style: italic; }

/* ── 数据标签 ── */
.dl { font-size: 8px; font-weight: 700; text-transform: uppercase; letter-spacing: .5px; color: rgba(139,148,158,0.4); margin-bottom: 4px; }

/* ── 辅助 ── */
.c-red { color: #f85149; } .fw7 { font-weight: 700; }
.mt8 { margin-top: 8px; } .mt12 { margin-top: 12px; }
.footer { text-align: center; color: #8b949e; font-size: 11px; padding: 40px 0 0; }

/* ── 系统概览缩略图（flow-chain · 宏观）── */
.overview { max-width: 1100px; margin: 0 auto 40px; padding: 16px 12px; border: 1px solid rgba(48,54,61,0.3); border-radius: 14px; background: rgba(22,27,34,0.3); }
.overview-title { font-size: 12px; font-weight: 600; color: #8b949e; margin-bottom: 12px; padding-left: 8px; border-left: 2px solid #58a6ff; }

.flow-chain { display: flex; align-items: stretch; gap: 0; }
.flow-chain .flat-node { flex: 1; min-width: 0; border-radius: 10px; padding: 10px 14px; text-align: center; transition: transform 0.2s; }
.flow-chain .flat-node:hover { transform: translateY(-2px); }
.flat-node .node-icon { font-size: 22px; margin-bottom: 4px; }
.flat-node .node-title { font-size: 13px; font-weight: 700; margin-bottom: 2px; }
.flat-node .node-desc { font-size: 10px; opacity: 0.7; line-height: 1.4; }
.flat-llm     { background: linear-gradient(135deg,#4a1a2a,#551a30); border: 1px solid #f85149aa; }
.flat-llm .node-title { color: #f85149; }
.flat-agent   { background: linear-gradient(135deg,#2a1a4a,#351a55); border: 1px solid #bc8cffaa; }
.flat-agent .node-title { color: #bc8cff; }
.flat-desktop { background: linear-gradient(135deg,#3a2a0a,#4a3a10); border: 1px solid #f0883eaa; }
.flat-desktop .node-title { color: #f0883e; }
.flat-topo    { background: linear-gradient(135deg,#1a3a2a,#1a4a30); border: 1px solid #7ee78788; }
.flat-topo .node-title { color: #7ee787; }
.flat-gap     { background: linear-gradient(135deg,#3a1a1a,#4a1a1a); border: 2px dashed #f85149aa; }
.flat-gap .node-title { color: #f85149; }

.chain-link {
  display: flex; flex-direction: column; align-items: center;
  justify-content: center; flex-shrink: 0; padding: 0 8px; min-width: 50px;
}
.chain-line {
  width: 100%; height: 2px; background: linear-gradient(90deg, #8b949e88, #58a6ff);
  position: relative; overflow: visible;
}
.chain-line::after {
  content: ''; position: absolute; right: -5px; top: 50%; transform: translateY(-50%);
  border-left: 7px solid #58a6ff; border-top: 4px solid transparent; border-bottom: 4px solid transparent;
}
.chain-line::before {
  content: ''; position: absolute; top: -2px; width: 6px; height: 6px;
  background: #fff; border-radius: 50%;
  box-shadow: 0 0 4px 2px rgba(255,255,255,0.45);
  animation: flowHoriz 2s ease-in-out infinite;
}
.chain-label { font-size: 8px; font-weight: 600; color: #8b949e; margin-top: 4px; text-align: center; white-space: nowrap; }
.chain-link.chain-gap .chain-line { background: linear-gradient(90deg, #8b949e88, #f85149); }
.chain-link.chain-gap .chain-line::after { border-left-color: #f85149; }
.chain-link.chain-gap .chain-line::before { box-shadow: 0 0 4px 2px rgba(248,81,73,0.55); }
.chain-link.chain-gap .chain-label { color: #f85149; }
```
