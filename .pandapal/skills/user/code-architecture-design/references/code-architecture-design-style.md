# 架构图 HTML 风格指南

> 本文件是项目唯一风格来源。生成 HTML 时直接引用本文件中的完整 CSS 模板，**不依赖任何产物 HTML 文件**。

## 零、关于风格

本文件为 code-architecture-design 技能提供 cards 富卡片风格。

---

## 一、设计原则

| 原则 | 说明 |
|------|------|
| **暗黑主题** | 背景 `#0f1117`，层次靠 surface `#1a1d27` / surface2 `#22263a` 区分 |
| **单文件自洽** | 所有样式内联在 `<style>` 中，无外部依赖，可直接用浏览器打开 |
| **信息密度适中** | 字体 10–13px，卡片 padding 10–20px，不追求大图留白 |
| **颜色有语义** | 蓝=数据流/链路，绿=成功/写入，橙=警告/触发，紫=策略/分类，青=召回/查询，粉=输出/结果，黄=配置/注意 |
| **动效克制** | 静态展示，不使用动画 |

---

## 二、CSS 变量系统

```css
:root {
  --bg:      #0f1117;   /* 页面底色 */
  --surface: #1a1d27;   /* 卡片底色 */
  --surface2: #22263a;  /* 次级面板、pipeline步骤 */
  --border:  #2e3352;   /* 通用边框 */
  --accent:  #4f8ef7;   /* 蓝色主色（数据流） */
  --accent2: #7c3aed;   /* 紫色（策略/分类） */
  --green:   #22c55e;
  --orange:  #f97316;
  --red:     #ef4444;
  --yellow:  #eab308;
  --teal:    #14b8a6;
  --pink:    #ec4899;
  --text:    #e2e8f0;
  --text2:   #94a3b8;   /* 次要文字 */
  --text3:   #64748b;   /* 占位/标签灰 */
}
```

---

## 三、核心组件库

### 3.1 页头（Header）

```html
<div class="header">
  <h1>标题文字</h1>
  <div class="subtitle">副标题 · 说明文字</div>
  <div class="phase-badges">
    <span class="badge badge-done">Phase 1 ✅</span>
    <span class="badge badge-purple">execution_strategy = parallel</span>
    <span class="badge badge-teal">asyncio.gather 并行</span>
  </div>
</div>
```

**H1 渐变色**（三选一，按色系选择）：
```css
/* 蓝紫粉 — 适合 SDK 概览 */
background: linear-gradient(135deg, #4f8ef7, #7c3aed, #ec4899);
/* 紫粉橙 — 适合技术架构、多模块协作 */
background: linear-gradient(135deg, #7c3aed, #ec4899, #f97316);
/* 蓝紫橙 — 适合部署拓扑、基础设施 */
background: linear-gradient(135deg, #58a6ff, #bc8cff, #f0883e);

-webkit-background-clip: text;
-webkit-text-fill-color: transparent;
background-clip: text;
```

### 3.2 Section 标签

```html
<div class="section-label">整体信息流转</div>
```

```css
.section-label {
  font-size: 11px; font-weight: 700;
  letter-spacing: 1px; text-transform: uppercase;
  color: var(--text3); margin-bottom: 10px; padding-left: 4px;
}
```

### 3.3 卡片（Card）

```html
<div class="card">
  <div class="card-title">
    <span class="dot" style="background:var(--teal)"></span>
    标题文字
  </div>
  <!-- 内容 -->
</div>
```

```css
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 20px; }
.card-title { font-size:13px; font-weight:700; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
.card-title .dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
```

彩色 card 边框（突出某张卡片）：
```html
<div class="card" style="border-color:rgba(20,184,166,0.3)">
```

### 3.4 Zone 包装器

用于包裹一整个功能区块（比 card 更大）：

```html
<div class="zone" style="border-color:rgba(236,72,153,0.25)">
  <div class="zone-header">
    <span class="icon">🔄</span>
    <span class="c-pink">标题</span>
  </div>
  <!-- 内容 -->
</div>
```

### 3.5 水平 Pipeline（流程步骤）

```html
<div class="pipeline">
  <div class="pipeline-step" style="border-color:rgba(79,142,247,0.4);background:rgba(79,142,247,0.06)">
    <div class="step-num">① Pre-Process</div>
    <div class="step-name c-blue">安全过滤</div>
    <div class="step-detail">内容安全·PII脱敏</div>
  </div>
  <div class="pipeline-arrow">›</div>
  <!-- 更多步骤 -->
</div>
```

**每步颜色规律**（按语义分配）：

| 步骤类型 | 边框色 | 背景色 | 文字类 |
|----------|--------|--------|--------|
| 输入/触发 | `rgba(124,58,237,0.4)` | `..,0.06)` | `c-purple` |
| 处理/链路 | `rgba(79,142,247,0.4)` | `..,0.06)` | `c-blue` |
| 召回/查询 | `rgba(20,184,166,0.4)` | `..,0.06)` | `c-teal` |
| 执行/写入 | `rgba(34,197,94,0.4)` | `..,0.06)` | `c-green` |
| 后处理/过滤 | `rgba(249,115,22,0.4)` | `..,0.06)` | `c-orange` |
| 输出/结果 | `rgba(236,72,153,0.4)` | `..,0.06)` | `c-pink` |

### 3.6 垂直 Pipeline

```html
<div class="v-pipeline">
  <div class="pipeline-step" style="border-color:rgba(20,184,166,0.3)">
    <div class="step-name" style="font-size:11px">步骤名</div>
    <div class="step-detail">详情</div>
  </div>
  <div class="flow-arrow">↓</div>
  <!-- 更多步骤 -->
</div>
```

### 3.7 DB Box（数据/存储展示）

```html
<div class="db-box">
  <div class="db-title">📄 ExperiencePattern</div>
  <div class="db-item"><strong>pattern_id</strong>  UUID hex · 主键</div>
  <div class="db-item"><strong>confidence</strong>  0.0 – 1.0</div>
</div>
```

彩色 db-box 边框：`style="border-color:rgba(20,184,166,0.25)"`

### 3.8 KV List（键值对列表）

```html
<ul class="kv-list">
  <li>
    <span class="kv-key">min_rounds</span>
    <span class="kv-val c-yellow small">2</span>
  </li>
</ul>
```

### 3.9 Info Box（高亮提示框）

```html
<div class="info-box purple">
  <span class="fw7 c-purple">⚡ 标题</span>
  <span class="c-text2 small" style="margin-left:8px">说明文字</span>
</div>
```

颜色类：`blue` · `green` · `purple` · `orange` · `teal` · `pink` · `yellow` · `red`

### 3.10 Code Block（代码展示）

```html
<div class="code-block">
  <span class="kw">await</span> learner.<span class="fn">start</span>()
  <span class="cm"># 注释</span>
  <span class="st">"字符串"</span>
</div>
```

```css
.code-block { background:var(--surface2); border:1px solid var(--border); border-radius:8px; padding:10px 14px; font-family:"SF Mono","Fira Code",monospace; font-size:10px; line-height:1.8; color:var(--text2); }
.code-block .kw { color: var(--accent2); }  /* 关键字 紫色 */
.code-block .fn { color: var(--accent); }   /* 函数名 蓝色 */
.code-block .st { color: var(--green); }    /* 字符串 绿色 */
.code-block .cm { color: var(--text3); }    /* 注释 灰色 */
```

### 3.11 Row / Col 布局

```html
<div class="row">
  <div class="col"><!-- 等宽列 --></div>
  <div class="col" style="flex:0.7"><!-- 窄列 --></div>
</div>
```



### 3.12 Conf Track（状态 / 生命周期轨道）

适合在卡片内展示状态流转、生命周期阶段、审批路径——比纯文字更直观，比流程图更轻量。

```html
<div class="conf-track">
  <div class="conf-step active">OPEN</div>
  <div class="conf-arrow">→</div>
  <div class="conf-step">CLAIMED</div>
  <div class="conf-arrow">→</div>
  <div class="conf-step done">COMPLETED</div>
  <div class="conf-arrow">→</div>
  <div class="conf-step dead">DEAD</div>
</div>
```

**状态色语义：**

| 修饰类 | 颜色 | 语义 |
|--------|------|------|
| （无） | 灰色默认 | 普通中间态 |
| `.active` | 蓝色 | 当前进行中 |
| `.done` | 绿色 | 成功终态 |
| `.warn` | 黄色 | 等待/警告态 |
| `.dead` | 红色 | 失败/终止终态 |

### 3.13 Sec Item（带图标安全/规则条目）

适合展示一组平行的规则、工具列表、权限条目——比纯段落更结构化，比 db-item 更有视觉层次。

```html
<div class="sec-item">
  <span class="sec-icon">🔒</span>
  <div>
    <div class="sec-label">条目标题 <code>tool_name</code></div>
    <div class="sec-desc">详细说明，可含 <code>inline code</code></div>
  </div>
</div>
```

结合 `db-box` 做整组容器：

```html
<div class="db-box" style="border-color:rgba(239,68,68,0.25)">
  <div class="db-title">🚫 拒绝执行</div>
  <div class="sec-item">
    <span class="sec-icon c-red">✗</span>
    <div>
      <div class="sec-label c-red">delete_file</div>
      <div class="sec-desc">删除文件，无法恢复</div>
    </div>
  </div>
  <!-- 更多条目 -->
</div>
```

### 3.14 Node Box（组件节点框）

适合在 cards 风格内展示带文件路径和标签的组件节点（比 flat-node 更轻，不用渐变背景，融入卡片布局）。

```html
<div class="node-box" style="border-color:rgba(79,142,247,0.3)">
  <div class="node-icon">📦</div>
  <div class="node-name">ComponentName</div>
  <div class="node-file">path/to/file.py</div>
  <div class="node-tags">
    <span class="node-tag">async</span>
    <span class="node-tag">singleton</span>
  </div>
</div>
```

多节点横排（搭配 `panelist-grid`）：

```html
<div class="panelist-grid">
  <div class="node-box"><div class="node-icon">🗄️</div><div class="node-name">VectorStore</div><div class="node-file">rag/vector_store.py</div></div>
  <div class="node-box"><div class="node-icon">📊</div><div class="node-name">BM25Store</div><div class="node-file">rag/bm25_store.py</div></div>
  <div class="node-box"><div class="node-icon">🕸️</div><div class="node-name">GraphStore</div><div class="node-file">rag/graph_store.py</div></div>
</div>
```

### 3.15 Rel Arrow（关系连线，带标签）

用于模块/卡片/分层之间的箭头连线，必须附带关系描述文字。支持水平和垂直两种方向。

**水平连线**（卡片之间）：

```html
<div class="rel-arrow">
  <span class="rel-label c-blue fw7">REST 调用</span>
  <span class="rel-symbol">→</span>
</div>
```

**垂直连线**（zone 分层之间）：

```html
<div class="rel-arrow down">
  <span class="rel-label c-purple fw7">gRPC 调用</span>
  <span class="rel-symbol">↓</span>
</div>
```

**与 pipeline 组合**（替换 `pipeline-arrow` 为 `rel-arrow` 以携带标签）：

```html
<div class="pipeline">
  <div class="pipeline-step"><!-- 模块 A --></div>
  <div class="rel-arrow">
    <span class="rel-label c-teal fw7">消息推送</span>
    <span class="rel-symbol">→</span>
  </div>
  <div class="pipeline-step"><!-- 模块 B --></div>
</div>
```

**常用关系标签参考**：`REST 调用` · `gRPC 调用` · `消息推送` · `事件触发` · `读写` · `订阅` · `SQL 查询` · `回调` · `轮询` · `文件传输`

**颜色按语义**：蓝=数据流调用，紫=策略触发，青=查询连接，绿=写入链路，橙=异步消息，粉=输出回调

---

## 四、颜色工具类（全局可用）

```css
/* 文字颜色 */
.c-blue   { color: #4f8ef7; }   .c-green  { color: #22c55e; }
.c-orange { color: #f97316; }   .c-yellow { color: #eab308; }
.c-teal   { color: #14b8a6; }   .c-pink   { color: #ec4899; }
.c-purple { color: #7c3aed; }   .c-gray   { color: #64748b; }
.c-text2  { color: #94a3b8; }

/* 背景色 */
.bg-blue   { background: rgba(79,142,247,0.12);  }
.bg-green  { background: rgba(34,197,94,0.12);   }
.bg-orange { background: rgba(249,115,22,0.12);  }
.bg-purple { background: rgba(124,58,237,0.12);  }
.bg-teal   { background: rgba(20,184,166,0.12);  }
.bg-yellow { background: rgba(234,179,8,0.12);   }

/* 排版辅助 */
.fw7   { font-weight: 700; }
.small { font-size: 10px; }
.mono  { font-family: "SF Mono","Fira Code",monospace; }
.mt4   { margin-top: 4px;  }  .mt8  { margin-top: 8px;  }  .mt12 { margin-top: 12px; }
```

---

## 五、页面整体骨架

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>模块名称 — 功能描述</title>
  <style>
    /* ① 粘贴完整 CSS 变量 + 组件样式 */
  </style>
</head>
<body>

<!-- Header -->
<div class="header">
  <h1>标题</h1>
  <div class="subtitle">路径说明 · 一句话概述</div>
  <div class="phase-badges">
    <span class="badge badge-purple">关键词 1</span>
    <span class="badge badge-teal">关键词 2</span>
  </div>
</div>

<div class="arch-root">

  <!-- Section 1 -->
  <div>
    <div class="section-label">流程概览</div>
    <div class="zone">
      <div class="zone-header"><span class="icon">🔄</span><span>说明</span></div>
      <div class="pipeline"><!-- 步骤 --></div>
      <div class="mt12 info-box blue"><!-- 补充说明 --></div>
    </div>
  </div>

  <!-- Section 2：多列卡片 -->
  <div>
    <div class="section-label">核心组件</div>
    <div class="row">
      <div class="col"><div class="card"><!-- ... --></div></div>
      <div class="col"><div class="card"><!-- ... --></div></div>
      <div class="col"><div class="card"><!-- ... --></div></div>
    </div>
  </div>

  <!-- Section 3：数据结构 -->
  <div>
    <div class="section-label">数据结构</div>
    <div class="row">
      <div class="col"><div class="card"><div class="db-box"><!-- ... --></div></div></div>
      <div class="col"><div class="card"><ul class="kv-list"><!-- ... --></ul></div></div>
    </div>
  </div>

</div>
</body>
</html>
```

---

## 六、适用场景

| 场景 | 推荐风格 | CSS 来源 |
|------|----------|----------|
| SDK / 子系统详解（模块 + 组件 + API） | cards 富卡片 | §九 cards 完整 CSS |
| 工作流 / 多 Agent 协作 | cards 富卡片 | §九 cards 完整 CSS |
| 部署拓扑 | cards 富卡片 | §九 cards 完整 CSS |

---

## 八、快速复用 checklist


- [ ] 复制完整 `<style>` 块（CSS 变量 + 所有组件）
- [ ] 设置 `<title>` 和 header 文字
- [ ] 选择 h1 渐变色（匹配主题色调）
- [ ] 按"流程概览 → 核心组件 → 数据结构 → 集成关系"顺序组织 section
- [ ] 流程步骤按颜色语义分配（紫=触发，蓝=处理，青=查询，绿=写入/成功，橙=后处理，粉=输出）
- [ ] card 彩色边框用 `style="border-color:rgba(x,x,x,0.3)"` 局部高亮
- [ ] info-box 用于补充说明（不超过 3 行，颜色匹配所在区块主色）
- [ ] 代码示例放 `.code-block`，用 `.kw/.fn/.st/.cm` 做语法高亮
- [ ] 状态机 / 生命周期用 `.conf-track`，搭配 `.active/.done/.warn/.dead` 色语义
- [ ] 规则/工具/权限列表用 `.sec-item`，比 db-item 多一层图标+描述结构
- [ ] 多组件拓扑节点用 `.node-box`（轻量卡片节点）替代纯文字枚举
- [ ] 模块/层之间的连线用 `.rel-arrow` 带标签，标注调用关系（如「REST 调用」「gRPC 调用」），不可只放光杆箭头
- [ ] 末尾加 footer 一行说明（文字颜色 `var(--text3)`，`font-size:11px`）



---

## 九、cards 富卡片 — 完整 `<style>` 模板

> 生成富卡片 HTML 时，**直接将下方 `<style>` 块整体粘贴**到 `<head>` 中，不需要再读取任何其他文件。

```css
/* ── cards 富卡片 完整样式 ── */
:root {
  --bg:      #0f1117;
  --surface: #1a1d27;
  --surface2: #22263a;
  --border:  #2e3352;
  --accent:  #4f8ef7;   /* 蓝色主色（数据流/处理） */
  --accent2: #7c3aed;   /* 紫色（策略/触发） */
  --green:   #22c55e;   /* 写入/成功 */
  --orange:  #f97316;   /* 后处理/警告 */
  --red:     #ef4444;
  --yellow:  #eab308;   /* 配置/注意 */
  --teal:    #14b8a6;   /* 查询/召回 */
  --pink:    #ec4899;   /* 输出/结果 */
  --text:    #e2e8f0;
  --text2:   #94a3b8;
  --text3:   #64748b;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: "SF Pro Display", "PingFang SC", "Segoe UI", system-ui, sans-serif;
  font-size: 13px;
  line-height: 1.5;
  padding: 32px 24px 64px;
}

/* ── Header ── */
.header { text-align: center; margin-bottom: 48px; }
.header h1 {
  font-size: 28px; font-weight: 700; letter-spacing: -0.5px;
  /* h1 渐变三选一（按主题色调选择）:
     蓝→紫→粉（SDK 概览）: linear-gradient(135deg, #4f8ef7, #7c3aed, #ec4899)
     紫→粉→橙（技术架构）: linear-gradient(135deg, #7c3aed, #ec4899, #f97316)
  */
  background: linear-gradient(135deg, #4f8ef7, #7c3aed, #ec4899);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}
.header .subtitle { color: var(--text2); margin-top: 6px; font-size: 14px; }
.phase-badges { display: flex; justify-content: center; gap: 8px; margin-top: 16px; flex-wrap: wrap; }
.badge { padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; letter-spacing: 0.3px; }
.badge-purple { background: rgba(124,58,237,0.15); color: #a78bfa; border: 1px solid rgba(124,58,237,0.3); }
.badge-teal   { background: rgba(20,184,166,0.15);  color: #2dd4bf; border: 1px solid rgba(20,184,166,0.3); }
.badge-blue   { background: rgba(79,142,247,0.15);  color: #7db5ff; border: 1px solid rgba(79,142,247,0.3); }
.badge-orange { background: rgba(249,115,22,0.15);  color: #fb923c; border: 1px solid rgba(249,115,22,0.3); }
.badge-green  { background: rgba(34,197,94,0.15);   color: #4ade80; border: 1px solid rgba(34,197,94,0.3); }
.badge-yellow { background: rgba(234,179,8,0.15);   color: #fbbf24; border: 1px solid rgba(234,179,8,0.3); }
.badge-pink   { background: rgba(236,72,153,0.15);  color: #f9a8d4; border: 1px solid rgba(236,72,153,0.3); }
.badge-done   { background: rgba(34,197,94,0.15);   color: #4ade80; border: 1px solid rgba(34,197,94,0.3); }

/* ── Layout ── */
.arch-root { display: flex; flex-direction: column; gap: 24px; max-width: 1300px; margin: 0 auto; }

/* ── Section label ── */
.section-label {
  font-size: 11px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
  color: var(--text3); margin-bottom: 10px; padding-left: 4px;
}

/* ── Card ── */
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 20px; }
.card-title { font-size: 13px; font-weight: 700; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
.card-title .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }

/* ── Row / Col ── */
.row { display: flex; gap: 16px; align-items: stretch; }
.col { flex: 1; min-width: 0; }

/* ── Zone ── */
.zone { border: 1px solid var(--border); border-radius: 16px; padding: 20px; background: var(--surface); }
.zone-header { font-size: 14px; font-weight: 700; margin-bottom: 16px; display: flex; align-items: center; gap: 10px; }
.zone-header .icon { font-size: 18px; }

/* ── Pipeline（水平）── */
.pipeline { display: flex; gap: 0; align-items: stretch; }
.pipeline-step {
  flex: 1; background: var(--surface2); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 12px; text-align: center;
}
.pipeline-step .step-num { font-size: 10px; font-weight: 700; color: var(--text3); margin-bottom: 4px; }
.pipeline-step .step-name { font-size: 12px; font-weight: 600; color: var(--text); }
.pipeline-step .step-detail { font-size: 10px; color: var(--text3); margin-top: 4px; line-height: 1.5; }
.pipeline-arrow { display: flex; align-items: center; padding: 0 6px; color: var(--text3); font-size: 14px; flex-shrink: 0; }

/* ── Rel Arrow（关系连线，带标签）── */
.rel-arrow {
  display: flex; align-items: center; justify-content: center;
  gap: 4px; flex-shrink: 0; padding: 0 6px;
}
.rel-arrow.down { flex-direction: column; gap: 2px; padding: 4px 0; }
.rel-label { font-size: 9px; color: var(--text3); white-space: nowrap; font-weight: 600; letter-spacing: 0.3px; }
.rel-symbol { font-size: 14px; color: var(--text3); line-height: 1; }
.rel-arrow.down .rel-symbol { font-size: 16px; }

/* ── Vertical Pipeline ── */
.v-pipeline { display: flex; flex-direction: column; gap: 6px; }
.flow-arrow { text-align: center; color: var(--text3); font-size: 20px; line-height: 1; padding: 2px 0; }

/* ── DB Box ── */
.db-box { border: 1px dashed var(--border); border-radius: 8px; padding: 10px 14px; background: var(--surface2); }
.db-title { font-size: 11px; font-weight: 700; color: var(--text2); margin-bottom: 6px; display: flex; align-items: center; gap: 6px; }
.db-item { font-size: 10px; color: var(--text3); padding: 2px 0; }
.db-item strong { color: var(--text2); }

/* ── KV List ── */
.kv-list { list-style: none; }
.kv-list li { display: flex; justify-content: space-between; align-items: flex-start; padding: 5px 0; border-bottom: 1px solid var(--border); gap: 8px; }
.kv-list li:last-child { border-bottom: none; }
.kv-key { color: var(--text2); font-size: 11px; min-width: 100px; flex-shrink: 0; }
.kv-val { color: var(--text); font-size: 11px; text-align: right; }

/* ── Info Box ── */
.info-box { border-radius: 8px; padding: 10px 14px; font-size: 11px; line-height: 1.6; }
.info-box.blue   { background: rgba(79,142,247,0.08);  border: 1px solid rgba(79,142,247,0.2);  }
.info-box.green  { background: rgba(34,197,94,0.08);   border: 1px solid rgba(34,197,94,0.2);   }
.info-box.purple { background: rgba(124,58,237,0.08);  border: 1px solid rgba(124,58,237,0.2);  }
.info-box.orange { background: rgba(249,115,22,0.08);  border: 1px solid rgba(249,115,22,0.2);  }
.info-box.teal   { background: rgba(20,184,166,0.08);  border: 1px solid rgba(20,184,166,0.2);  }
.info-box.pink   { background: rgba(236,72,153,0.08);  border: 1px solid rgba(236,72,153,0.2);  }
.info-box.yellow { background: rgba(234,179,8,0.08);   border: 1px solid rgba(234,179,8,0.2);   }
.info-box.red    { background: rgba(239,68,68,0.08);   border: 1px solid rgba(239,68,68,0.2);   }

/* ── Code Block ── */
.code-block {
  background: var(--surface2); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 14px; font-family: "SF Mono","Fira Code",monospace; font-size: 10px; line-height: 1.8; color: var(--text2);
}
.code-block .kw { color: var(--accent2); }  /* 关键字 紫色 */
.code-block .fn { color: var(--accent); }   /* 函数名 蓝色 */
.code-block .st { color: var(--green); }    /* 字符串 绿色 */
.code-block .cm { color: var(--text3); }    /* 注释 灰色 */

/* ── Panelist Grid（多面板布局）── */
.panelist-grid { display: flex; gap: 10px; }
.panelist-card { flex: 1; border-radius: 10px; padding: 12px 14px; border: 1px solid var(--border); background: var(--surface2); }
.panelist-card .p-name { font-size: 12px; font-weight: 700; margin-bottom: 4px; }
.panelist-card .p-role { font-size: 10px; color: var(--text3); line-height: 1.5; }

/* ── Conf Track（状态/生命周期轨道）── */
.conf-track { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.conf-step {
  padding: 3px 10px; border-radius: 12px; font-size: 10px; font-weight: 700;
  background: var(--surface2); border: 1px solid var(--border); color: var(--text3);
  font-family: "SF Mono","Fira Code",monospace;
}
.conf-step.active { background: rgba(79,142,247,0.12);  border-color: rgba(79,142,247,0.4);  color: var(--accent);  }
.conf-step.done   { background: rgba(34,197,94,0.12);   border-color: rgba(34,197,94,0.4);   color: var(--green);   }
.conf-step.warn   { background: rgba(234,179,8,0.12);   border-color: rgba(234,179,8,0.4);   color: var(--yellow);  }
.conf-step.dead   { background: rgba(239,68,68,0.12);   border-color: rgba(239,68,68,0.4);   color: var(--red);     }
.conf-arrow { color: var(--text3); font-size: 12px; flex-shrink: 0; }

/* ── Sec Item（带图标的规则/工具条目）── */
.sec-item { display: flex; align-items: flex-start; gap: 8px; padding: 5px 0; border-bottom: 1px solid var(--border); }
.sec-item:last-child { border-bottom: none; }
.sec-icon { font-size: 13px; flex-shrink: 0; margin-top: 1px; }
.sec-label { font-size: 11px; font-weight: 700; color: var(--text2); }
.sec-desc  { font-size: 10px; color: var(--text3); margin-top: 2px; line-height: 1.5; }

/* ── Node Box（轻量组件节点框）── */
.node-box {
  border: 1px solid var(--border); border-radius: 10px;
  padding: 10px 12px; background: var(--surface2); text-align: center;
  flex: 1;
}
.node-box .node-icon { font-size: 20px; margin-bottom: 4px; }
.node-box .node-name { font-size: 12px; font-weight: 700; color: var(--text); }
.node-box .node-file { font-size: 9px; color: var(--text3); font-family: "SF Mono","Fira Code",monospace; margin-top: 3px; }
.node-box .node-tags { display: flex; justify-content: center; gap: 4px; flex-wrap: wrap; margin-top: 5px; }
.node-tag { padding: 1px 6px; border-radius: 8px; font-size: 9px; font-weight: 600;
  background: var(--surface); border: 1px solid var(--border); color: var(--text3); }

/* ── 颜色工具类 ── */
.c-blue   { color: var(--accent);  }  .c-green  { color: var(--green);   }
.c-orange { color: var(--orange);  }  .c-yellow { color: var(--yellow);  }
.c-teal   { color: var(--teal);    }  .c-pink   { color: var(--pink);    }
.c-purple { color: var(--accent2); }  .c-gray   { color: var(--text3);   }
.c-red    { color: var(--red);     }  .c-text2  { color: var(--text2);   }
.bg-blue   { background: rgba(79,142,247,0.12);  }  .bg-green  { background: rgba(34,197,94,0.12);  }
.bg-orange { background: rgba(249,115,22,0.12);  }  .bg-purple { background: rgba(124,58,237,0.12); }
.bg-teal   { background: rgba(20,184,166,0.12);  }  .bg-yellow { background: rgba(234,179,8,0.12);  }
.fw7  { font-weight: 700; }  .small { font-size: 10px; }  .mono { font-family: "SF Mono","Fira Code",monospace; }
.mt4  { margin-top: 4px;  }  .mt8  { margin-top: 8px;  }  .mt12 { margin-top: 12px; }
```

