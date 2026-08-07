---
name: data-flow-design
description: >
  将需求描述或源码转换为全景数据流 HTML 图。
when_to_use: >
  当用户说"数据流"、"信息流转"、"数据怎么跑"、"端到端链路"、"链路追踪"、"画数据流图"时调用。
  当要追踪一条用户操作的完整路径、标注数据在各环节的变形时使用。
  输入可以是描述文字、文件路径或模块名。
---

# data-flow-design — 生成端到端数据流全景图

---

## 用法

```
$ARGUMENTS
```

`$ARGUMENTS` 是要生成数据流图的描述文字、文件路径或模块名。

**示例：**
- `用户删除Skill数据流：前端→IPC→后端→文件系统→返回`
- `用户发聊天消息的端到端链路`
- `消息从 MQTT 到达 → Python 后端处理 → LLM 调用 → 回复推送的完整数据流`
- `assistant/perception/` — 读目录源码生成数据流图

---

## 核心原则

> **五元组不可丢**：每个节点 = 文件路径 + 类名.方法名 + 数据结构（入参/出参）+ 自然语言描述 + 协议通道。看完图就能直接在代码里定位。
> **全景图**：垂直主链 + 可插拔的分支/子链，上下左右不局限。
> **数据结构必须展示**：任何有数据结构的环节都用 `fd`（pre+语法高亮）展示，必须标注归属（哪个类的哪个方法的入参/出参），不写成裸 JSON。

---

## 执行步骤

### Step 1：理解输入

路径约定：所有相对路径基于项目根目录。


如果 `$ARGUMENTS` 看起来像文件/目录路径（含 `/` 或 `\` 或扩展名）：
- 读取目标文件，提取模块间的数据流转逻辑
- 如果是目录，glob `**/*.py` 读取核心文件（≤15 个全部读，超出按文件名判断核心文件）
- 提取：**文件路径**、类名、方法签名、方法调用链、IPC/task/invoke 调用、import 依赖

否则 `$ARGUMENTS` 就是数据流描述文本：
- 优先搜索项目中已有模块名或类名校准命名和协议
- 搜不到则按描述文案直接生成

**信息充足性检查**（开始前必须确认，缺什么问什么）：

- **必须有的**：要追踪什么数据流（描述或链路名）？涉及哪些模块/系统？
- **必须有的**：现有代码上下文（项目路径？当前架构？）？关键约束（性能/技术栈/部署）？
- 信息不足 → `ask_user` 追问，直到能说出至少一条完整链路（起点→经过→终点）。

---

### Step 2：确定输出路径与加载样式

输出到 `docs/{name}/`，文件名从输入推断：
- 文件路径 → 取文件名去扩展名
- 描述文本 → 取前 2-3 个关键词
- 如: `docs/{name}/{name}_data-flow-design.html`

加载样式：读取 `references/data-flow-style.md`，取完整 CSS 模板。

---

### Step 3：组织数据流内容

#### 1. 选一条关键链路（端到端）

每张图只管一条用户操作的完整路径。不要贪多。

#### 2. 梳理节点与协议

识别链路中的关键模块（2-3 级粒度），确定模块间的协议通道：
- **哪些模块**：列出所有参与数据流转的模块，**必须标注文件路径 + 类名 + 方法名**，例如 `pandaren/skill/registry.py · SkillRegistry.search_skills()`
- **如何连接**：模块间是串行还是并行（需要 Fan-Out/Fan-In）
- **什么协议**：invoke / stdin JSON Line / emit("event") / zustand setState / HTTP POST / Method Chain

#### 3. 标注每步的五元组与数据格式

每个模块节点必须展示以下**五项，缺一不可**：

1. **文件路径**：相对项目根目录的路径，放在 `g-head .role`，如 `pandaren/skill/registry.py`
2. **类名.方法名()**：放在 `g-head .name`，如 `SkillRegistry.search_skills()`
3. **自然语言解释**（最重要，像代码注释一样）：每个节点和每个 sub-node 都必须有解释，**说清楚这步干了什么、为什么这么干、有什么意义**。让没看过代码的人也能看懂。放在 `g-body` 顶部用 `<p style="font-size:11px;color:#8b949e;line-height:1.7;margin-bottom:10px;">` 写完整解释，每个 fd 块前再加一行 `dl` 简要摘要。例如：
   ```
   <p style="font-size:11px;color:#8b949e;line-height:1.7;margin-bottom:10px;">
   LLM 在推理过程中发现用户意图匹配了某个 Skill，于是调用 search_skills 工具。
   这个调用会触发 Skill 的完整生命周期：查找匹配 → 门禁校验 → 渲染内容 → 注册为临时 Tool → 设置权限白名单。
   这是整个 Skill 系统的入口，后续所有步骤都由此触发。
   </p>
   ```
   解释要点：**做了什么 → 为什么需要这步 → 数据从哪变到哪 → 对下游有什么影响**。sub-node 和 gap 节点同理，每步都要解释清楚。
4. **入站/出站数据结构**：fd 结构化代码块（pre+语法高亮），**首行必须用注释标注归属**：
   `// pandaren/skill/registry.py · SkillRegistry.search_skills() → 入参`
   `// pandaren/skill/registry.py · SkillRegistry.search_skills() → 出参/返回值`
5. **协议通道**：箭头 + g-proto 标签（invoke / stdin JSON Line / emit("event") / zustand setState / HTTP POST / Method Chain 等）

如果有模块内部多步转换链，用 `sub-chain` 展开，**每个 sub-node 同样必须标注文件路径+类名.方法名**。

---

#### 4. 组织模板

1. **链路概述** — 一句话：什么操作、起点、终点
2. **系统概览缩略图**（`flow-chain` + `overview`）— 关键节点串联，宏观感受，**必须放在全景图上方**
3. **全景图**（`graph` 容器）：
   - 每个模块一个 `g-node`（class 按颜色语义）
   - 模块间用 `g-arrow`（协议箭头）+ 流动白点动画
   - 并行分支用 `fan-out` / `fan-in`
   - 模块内部子链用 `sub-chain`
4. **缺口标注** — 缺失环节必须三要素齐全：
   - **当前状态**：缺什么（用红色 `g-node` + `g-arrow gap`）
   - **需新增的数据结构**：具体字段、类型、示例值（用 `fd` 完整展示）
   - **修改位置**：在哪个模块的哪个方法/生命周期钩子中注入（文字说明）

#### 5. 输出检查

- [ ] 起点是**用户动作**，终点是**UI 更新**（闭环）
- [ ] 每个节点标注了**文件路径 + 类名.方法名()**（g-head .name + .role）
- [ ] 每个 fd 数据块前面有**自然语言描述**（dl 标签，一行话说明"谁调用谁、传了什么"）
- [ ] 每个 fd 数据块首行有**归属注释**（`// 文件路径 · 类名.方法名() → 入参/出参`）
- [ ] 每个箭头标注了**传输协议/通道**（`g-proto` 标签）
- [ ] 所有箭头有**流动白点动画**（flowVert / flowHoriz）
- [ ] 能看出数据在哪一步**发生了变形**
- [ ] 缺口标注含三要素：当前缺什么 + 需新增的数据结构（fd 完整展示）+ 修改位置
- [ ] 一张图 = 一条链路，未贪多

---

#### 6. 反例 vs 正例

| 问题 | 反例 ❌ | 正例 ✅ |
|---|---|---|
| 缺少文件路径 | `g-head .name="SkillRegistry"` `.role=""` — 不知道代码在哪 | `g-head .name="SkillRegistry.search_skills()"` `.role="pandaren/skill/registry.py"` |
| fd 无归属注释 | 裸 JSON，不知道谁产生的、谁消费的 | 首行 `// pandaren/skill/registry.py · SkillRegistry.search_skills() → 出参` |
| 缺自然语言描述 | 直接放 fd 代码块，看的人不知道这步在干什么 | fd 前加 `dl`："Agent 收到请求后通过 ToolContext 调用 search_skills，入参：" |
| 内容空洞 | `前端 → IPC → 后端` — 没格式、没协议、没结构 | 三元组各归其位：`{"type":"DELETE","skill_name":"x"}` + `stdin JSON Line` |

---

#### 7. 内容原则

- 不虚构不存在的模块名/类名
- 信息不足时标注 `[待确认]`
- 粒度到子系统的 2-3 级子模块，不拆过细
- 颜色语义：紫=入口，蓝=处理/匹配，橙=执行，绿=出口/UI，红=缺口

---

### Step 4：生成 HTML 文件

从 `references/data-flow-style.md` 取完整 `<style>` 块，嵌入 HTML。按以下结构组织 body：

```
overview (系统概览缩略图 · 必须在 graph 上方)
  └── flow-chain → flat-node × N + chain-link 箭头

graph 容器
├── g-row (single) → g-node (入口模块)
├── g-arrow (协议箭头)
├── [可选] fan-out → g-row (branch) → g-node × N → fan-in
├── g-arrow (协议箭头)
├── g-row (single) → g-node (后续模块)
├── ...
└── g-row (single) → g-node red (缺口)
```

每个节点：`g-head`（dot + `name="类名.方法名()"` + `role="文件路径"`）+ `g-body`（描述 dl + fd 数据块 · 可选 sub-chain）。
fd 数据块标准格式：首行用 `// 文件路径 · 类名.方法名() → 入参/出参` 标注归属。

缺口节点的 `g-body` 必须包含：
- 🟡 **当前状态**：说明哪一步缺失
- 🟢 **需新增的数据结构**：完整 fd 代码块，含字段名、类型、示例值
- 🔵 **修改位置**：模块名 → 方法名 → 注入点

---

### Step 5：写入文件并报告

写入目标路径，报告：
- 输出文件路径
- 数据流链路名称
- 节点数

---

## 模板与规范

全部在 `references/data-flow-style.md`：
- data-flow 全景图完整 CSS + 页面骨架
- **g-node** 节点卡片（谁+格式）
- **g-arrow** 协议箭头（协议+流动白点）
- **fan-out / fan-in** 并行分支连接器
- **sub-chain** 模块内部子步骤链
- **fd + 语法高亮** 数据结构展示
