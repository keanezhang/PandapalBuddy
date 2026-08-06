# Mermaid 流程图 HTML 模板与风格规范

> 本文件是 `flowchart` skill 的唯一样式来源。生成 HTML 时从此文件取完整模板。

---

## 一、HTML 页面模板

使用以下完整模板，替换 `{{标题}}`、`{{来源说明}}` 和 `{{MERMAID_CODE}}`。

「返回图集」按钮：检查输出目录是否存在 `index.html`，存在则保留 `<a class="btn" href="index.html">返回图集</a>`，不存在则移除该行。

```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{{标题}}</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
<style>
  :root {
    --bg:      #0f1117;
    --surface: #1a1d27;
    --border:  #2e3352;
    --accent:  #4f8ef7;
    --accent2: #7c3aed;
    --green:   #22c55e;
    --orange:  #f97316;
    --red:     #ef4444;
    --text:    #e2e8f0;
    --text2:   #94a3b8;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background-color: var(--bg);
    background-image:
      radial-gradient(circle, rgba(255,255,255,.03) 1px, transparent 1px);
    background-size: 20px 20px;
    color: var(--text);
    font-family: 'Inter', ui-sans-serif, system-ui, -apple-system, sans-serif;
    min-height: 100vh;
  }
  body::before {
    content: '';
    position: fixed; inset: 0;
    background: radial-gradient(900px 600px at 10% 5%, rgba(79,142,247,.06), transparent 60%),
                radial-gradient(800px 500px at 90% 10%, rgba(124,58,237,.05), transparent 55%),
                radial-gradient(700px 500px at 50% 90%, rgba(34,197,94,.03), transparent 55%);
    pointer-events: none; z-index: 0;
  }
  .wrap { position: relative; z-index: 1; max-width: 4000px; margin: 0 auto; padding: 40px 24px 20px; }
  .top { text-align: center; margin-bottom: 28px; }
  h1 {
    font-size: 28px; font-weight: 700; letter-spacing: -0.5px; margin: 0;
    background: linear-gradient(135deg, #60a0ff, #a78bfa, #fb923c);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  }
  .meta { font-size: 14px; color: var(--text2); margin-top: 10px; }
  .phase-badges { display: flex; justify-content: center; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
  .badge { padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; letter-spacing: 0.3px; }
  .badge-purple { background: rgba(124,58,237,0.15); color: #a78bfa; border: 1px solid rgba(124,58,237,0.3); }
  .badge-teal   { background: rgba(20,184,166,0.15);  color: #2dd4bf; border: 1px solid rgba(20,184,166,0.3); }
  .badge-blue   { background: rgba(79,142,247,0.15);  color: #7db5ff; border: 1px solid rgba(79,142,247,0.3); }
  .badge-orange { background: rgba(249,115,22,0.15);  color: #fb923c; border: 1px solid rgba(249,115,22,0.3); }
  .badge-green  { background: rgba(34,197,94,0.15);   color: #4ade80; border: 1px solid rgba(34,197,94,0.3); }
  .badge-pink   { background: rgba(236,72,153,0.15);  color: #f9a8d4; border: 1px solid rgba(236,72,153,0.3); }
  .badge-red    { background: rgba(239,68,68,0.15);   color: #f87171; border: 1px solid rgba(239,68,68,0.3); }
  .card {
    position: relative; z-index: 1;
    border: 1px solid rgba(255,255,255,.06);
    border-radius: 18px;
    background: rgba(15,17,23,.70);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    padding: 20px;
    overflow: auto;
    box-shadow: 0 0 0 1px rgba(255,255,255,.015), 0 32px 80px rgba(0,0,0,.55);
  }
  .card svg { display: block; max-width: 100%; height: auto; margin: 0 auto; }
  .error { color: #fca5a5; font-size: 14px; white-space: pre-wrap; padding: 12px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1>{{标题}}</h1>
    <div class="meta">{{来源说明}}</div>
    <!-- 可选：子图/阶段徽章，从 subgraph 标签名生成 -->
    {{PHASE_BADGES}}
  </div>
  <div class="card" id="out"></div>
</div>

<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
const outEl = document.getElementById("out");

const code = `{{MERMAID_CODE}}`;

mermaid.initialize({
  startOnLoad: false,
  securityLevel: "strict",
  theme: "dark",
  themeVariables: {
    fontFamily: '"Inter", ui-sans-serif, system-ui, sans-serif',
    fontSize: '30px',
    primaryColor: '#1a1d27',
    primaryTextColor: '#e2e8f0',
    primaryBorderColor: '#2e3352',
    lineColor: '#2e3352',
    secondaryColor: '#1a1d27',
    tertiaryColor: '#1a1d27',
  },
  flowchart: { useMaxWidth: true, htmlLabels: true, curve: "basis", padding: 0, nodeSpacing: 120, rankSpacing: 120 }
});

function setError(err) {
  outEl.innerHTML = "";
  const div = document.createElement("div");
  div.className = "error";
  div.textContent = String(err?.message || err || "渲染失败");
  outEl.appendChild(div);
}

function enhanceSvg(svgEl) {
  const NS = "http://www.w3.org/2000/svg";

  // 0. SVG 填满容器
  svgEl.removeAttribute("width");
  svgEl.removeAttribute("height");
  svgEl.style.setProperty("width", "100%", "important");
  svgEl.style.setProperty("height", "auto", "important");
  svgEl.setAttribute("preserveAspectRatio", "xMinYMin meet");

  // 1. 滤镜 + 渐变
  let defs = svgEl.querySelector("defs");
  if (!defs) {
    defs = document.createElementNS(NS, "defs");
    svgEl.insertBefore(defs, svgEl.firstChild);
  }
  const shadow = document.createElementNS(NS, "filter");
  shadow.setAttribute("id", "ns");
  shadow.setAttribute("x", "-10%"); shadow.setAttribute("y", "-10%");
  shadow.setAttribute("width", "130%"); shadow.setAttribute("height", "130%");
  shadow.innerHTML = '<feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="rgba(0,0,0,0.5)"/>';
  defs.appendChild(shadow);

  const gradients = {
    gMain:      [['0%','#60a0ff'],['100%','#4f8ef7']],
    gBranch:    [['0%','#fb8a30'],['100%','#f97316']],
    gException: [['0%','#f05555'],['100%','#ef4444']],
    gDecide:    [['0%','#9050ff'],['100%','#7c3aed']],
    gSuccess:   [['0%','#34e070'],['100%','#22c55e']],
  };
  for (const [id, stops] of Object.entries(gradients)) {
    const lg = document.createElementNS(NS, "linearGradient");
    lg.setAttribute("id", id); lg.setAttribute("x1","0"); lg.setAttribute("y1","0");
    lg.setAttribute("x2","1"); lg.setAttribute("y2","1");
    stops.forEach(([o,c]) => {
      const s = document.createElementNS(NS, "stop");
      s.setAttribute("offset", o); s.setAttribute("stop-color", c);
      lg.appendChild(s);
    });
    defs.appendChild(lg);
  }

  // 2. 节点处理：圆角 + 阴影 + 渐变描边 + 字号加大
  const strokeMap = {
    '#4f8ef7': 'gMain',
    '#f97316': 'gBranch',
    '#ef4444': 'gException',
    '#7c3aed': 'gDecide',
    '#22c55e': 'gSuccess',
  };
  const classTextColorMap = {
    'main':      '#7db5ff',
    'branch':    '#fb923c',
    'exception': '#f87171',
    'decide':    '#a78bfa',
    'success':   '#4ade80',
  };

  svgEl.querySelectorAll("rect.label-container, rect.basic").forEach(rect => {
    rect.setAttribute("rx", "12");
    rect.setAttribute("ry", "12");
    rect.setAttribute("filter", "url(#ns)");
    rect.setAttribute("stroke-width", "2.5");
    const w = parseFloat(rect.getAttribute("width")) || 0;
    const h = parseFloat(rect.getAttribute("height")) || 0;
    if (w > 0) rect.setAttribute("width", w + 30);
    if (h > 0) rect.setAttribute("height", h + 16);
    const sc = rect.getAttribute("stroke");
    const grad = strokeMap[sc];
    if (grad) rect.setAttribute("stroke", `url(#${grad})`);
  });

  svgEl.querySelectorAll("g.node").forEach(nodeGroup => {
    let textColor = null;
    for (const [cls, color] of Object.entries(classTextColorMap)) {
      if (nodeGroup.classList.contains(cls)) { textColor = color; break; }
    }
    nodeGroup.querySelectorAll('foreignObject').forEach(fo => {
      const fw = parseFloat(fo.getAttribute("width")) || 0;
      const fh = parseFloat(fo.getAttribute("height")) || 0;
      if (fw > 0) fo.setAttribute("width", fw + 30);
      if (fh > 0) fo.setAttribute("height", fh + 16);
      fo.style.setProperty("display", "flex", "important");
      fo.style.setProperty("align-items", "center", "important");
      fo.style.setProperty("justify-content", "center", "important");
    });
    nodeGroup.querySelectorAll('foreignObject div, foreignObject span').forEach(el => {
      el.style.setProperty('font-size', '30px', 'important');
      el.style.setProperty('line-height', '1.5', 'important');
      el.style.setProperty('font-weight', '500', 'important');
      el.style.setProperty('text-align', 'center', 'important');
      el.style.setProperty('display', 'block', 'important');
      el.style.setProperty('width', '100%', 'important');
      el.style.setProperty('box-sizing', 'border-box', 'important');
      if (textColor) el.style.setProperty('color', textColor, 'important');
    });
    nodeGroup.querySelectorAll('text, tspan').forEach(el => {
      el.setAttribute('font-size', '30px');
      el.setAttribute('text-anchor', 'middle');
      el.setAttribute('dominant-baseline', 'middle');
      if (textColor) el.setAttribute('fill', textColor);
    });
  });

  // 3. Subgraph 标题：加宽 foreignObject 防止文字截断
  svgEl.querySelectorAll("g.cluster").forEach(cluster => {
    cluster.querySelectorAll('foreignObject').forEach(fo => {
      const fw = parseFloat(fo.getAttribute("width")) || 0;
      const fh = parseFloat(fo.getAttribute("height")) || 0;
      if (fw > 0) fo.setAttribute("width", fw + 160);
      if (fh > 0) fo.setAttribute("height", fh + 50);
    });
    cluster.querySelectorAll('foreignObject div, foreignObject span, text, tspan').forEach(el => {
      if (el.tagName === 'text' || el.tagName === 'tspan') {
        el.setAttribute('font-size', '28px');
        el.setAttribute('font-weight', '600');
      } else {
        el.style.setProperty('font-size', '28px', 'important');
        el.style.setProperty('font-weight', '600', 'important');
        el.style.setProperty('color', '#94a3b8', 'important');
      }
    });
  });

  // 4. 连线标签
  svgEl.querySelectorAll(".edgeLabel").forEach(el => {
    el.querySelectorAll('foreignObject div, foreignObject span').forEach(child => {
      child.style.setProperty('font-size', '30px', 'important');
      child.style.setProperty('color', '#c4c4d4', 'important');
      child.style.setProperty('font-weight', '500', 'important');
    });
    el.querySelectorAll('text, tspan').forEach(child => {
      child.setAttribute('font-size', '30px');
      child.setAttribute('fill', '#c4c4d4');
    });
    el.querySelectorAll("rect").forEach(r => {
      r.setAttribute("fill", "none"); r.setAttribute("stroke", "none");
    });
  });

  // 5. Subgraph 背景：极淡 + 无边框 + 圆角
  svgEl.querySelectorAll("rect.cluster").forEach(rect => {
    rect.setAttribute("rx", "14");
    rect.setAttribute("ry", "14");
    rect.setAttribute("stroke", "none");
    rect.setAttribute("stroke-width", "0");
    rect.removeAttribute("filter");
    rect.setAttribute("fill-opacity", "0.35");
    const w = parseFloat(rect.getAttribute("width")) || 0;
    const h = parseFloat(rect.getAttribute("height")) || 0;
    if (w > 0) rect.setAttribute("width", w + 50);
    if (h > 0) rect.setAttribute("height", h + 50);
  });

  // 6. 连线 + 箭头
  svgEl.querySelectorAll(".edgePath path, .edge-pattern, g.edge path").forEach(p => {
    p.setAttribute("stroke", "#2e3352");
    p.setAttribute("stroke-width", "1.5");
  });
  svgEl.querySelectorAll("defs marker path").forEach(p => {
    p.setAttribute("fill", "#2e3352");
    p.setAttribute("stroke", "#2e3352");
  });

  // 7. 重新计算 viewBox：所有放大操作后，防止裁切
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  svgEl.querySelectorAll("rect, foreignObject, text, tspan").forEach(el => {
    const x = parseFloat(el.getAttribute("x")) || 0;
    const y = parseFloat(el.getAttribute("y")) || 0;
    const w = parseFloat(el.getAttribute("width")) || 0;
    const h = parseFloat(el.getAttribute("height")) || 0;
    if (w <= 0 || h <= 0) return;
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x + w);
    maxY = Math.max(maxY, y + h);
  });
  if (isFinite(minX)) {
    const margin = 60;
    svgEl.setAttribute("viewBox", `${minX - margin} ${minY - margin} ${maxX - minX + margin * 2} ${maxY - minY + margin * 2}`);
  }

  // 8. CSS 字体兜底
  const styleEl = document.createElementNS(NS, "style");
  styleEl.textContent = `
    .nodeLabel, .label foreignObject div {
      font-size: 30px !important;
      line-height: 1.5 !important;
      font-weight: 500 !important;
    }
    .cluster-label .nodeLabel, .cluster-label foreignObject div {
      font-weight: 600 !important;
      font-size: 28px !important;
      letter-spacing: .02em !important;
      color: #94a3b8 !important;
    }
    .edgeLabel rect { fill: none !important; stroke: none !important; }
    .edgeLabel foreignObject div, .edgeLabel span {
      font-size: 30px !important;
      color: #c4c4d4 !important;
      font-weight: 500 !important;
    }
  `;
  svgEl.appendChild(styleEl);
}

async function render() {
  try {
    const src = code.replace(/\r\n?/g, "\n");
    const id = "m" + Math.random().toString(16).slice(2);
    const { svg } = await mermaid.render(id, src);
    outEl.innerHTML = svg;
    const svgEl = outEl.querySelector("svg");
    if (svgEl) enhanceSvg(svgEl);
  } catch (e) {
    setError(e);
  }
}

render();
</script>
</body>
</html>
```

---

## 1.5、字号规范

| 元素 | 字号 | 说明 |
|------|------|------|
| 子图标题（cluster label） | **28px**，不可超过 | `enhanceSvg` 中 JS + CSS 双重锁定为 28px，同时 foreignObject 加宽+160、加高+50 防止截断 |
| 节点文字 | **30px** 起步 | `enhanceSvg` 中 JS 设 30px，CSS `.nodeLabel` 兜底 30px |
| 连线标签 | **30px** | `enhanceSvg` 中 JS 设 30px，CSS 兜底 30px |

> 不要在 Mermaid 代码中设置 `fontSize`。字号由 HTML 模板的 `enhanceSvg` 统一控制，Mermaid 初始化中的 `themeVariables.fontSize: '30px'` 仅作为初始值。

---

## 二、Mermaid 代码规范

### 图方向

- 默认 `flowchart TD`，横向数据流用 `flowchart LR`，节点多的时候可以混合使用

### 节点写法

```
flowchart TD
  NODE["显示文字<br/>换行用 <br/>"]
  DECIDE{"判断条件"}
  subgraph NAME["子图标签"]
    A --> B
  end
```

- 节点 ID 用简短大写英文，如 `LOGIN`、`AUTH_CHECK`、`REDIRECT_HOME`
- 方括号 `[...]` 用于矩形节点，花括号 `{...}` 用于判断/菱形节点

### 节点颜色体系（classDef）

必须使用以下 5 类 classDef，`enhanceSvg` 会自动匹配渐变描边和文字颜色：

| class | 语义 | fill | stroke | 渐变 ID |
|-------|------|------|--------|---------|
| `main` | 核心流程/入口节点 | `#1a1d27` | `#4f8ef7` 蓝 | `gMain` |
| `branch` | 分支操作/编辑节点 | `#1e1b1a` | `#f97316` 橙 | `gBranch` |
| `exception` | 异常/拒绝/失败 | `#201a1c` | `#ef4444` 红 | `gException` |
| `decide` | 判断/菱形节点 | `#1c1a28` | `#7c3aed` 紫 | `gDecide` |
| `success` | 成功/确认节点 | `#161f1c` | `#22c55e` 绿 | `gSuccess` |

必须这样写：
```
classDef main      fill:#1a1d27,stroke:#4f8ef7,stroke-width:2.5px
classDef branch    fill:#1e1b1a,stroke:#f97316,stroke-width:2.5px
classDef exception fill:#201a1c,stroke:#ef4444,stroke-width:2.5px
classDef decide    fill:#1c1a28,stroke:#7c3aed,stroke-width:2.5px
classDef success   fill:#161f1c,stroke:#22c55e,stroke-width:2.5px

class NODE1,NODE2,NODE3 main
class NODE4,NODE5 branch
...
```

> **重要**：classDef 的 stroke 颜色必须与上面表格完全一致，`enhanceSvg` 靠 `strokeMap` 匹配颜色来应用渐变。

### Subgraph 风格

每个 subgraph 添加极淡背景色，`stroke:none`：

```
style ENTRY fill:#13151c,stroke:none
style BROWSE fill:#13151c,stroke:none
```

### 连线

```
linkStyle default stroke:#2e3352,stroke-width:1.5px
```

关键判断分支加标签 `-->|"说明"|`，普通流程不加标签。

### 箭头与子图

- 每个 subgraph 对应一个逻辑层
- 子图内节点用 `subgraph` 包裹
- 确保每条路径闭环（每个分支都有下一步或终点）

### 方法论

1. **识别起止点**：入口/触发 → 成功/失败/返回
2. **关键决策点**：菱形节点 `{...}`，只画影响后续路径的分支
3. **控制粒度**：5~15 个节点最佳，超出用 subgraph 分层
4. **闭合路径**：每个分支必须有明确下一步，禁止悬空箭头
5. **少交叉**：连线交叉可能意味着逻辑不清晰，反思是否可以重组布局
