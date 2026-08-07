---
name: flowchart
description: 将需求描述、源码或文档转换为 Mermaid 流程图 HTML 文件。当用户说"生成流程图"、"画流程图"时调用。
when_to_use: 当要可视化流程、交互流程、架构关系时使用。输入可以是描述文本、文件路径或模块名。
---

# flowchart — 生成 Mermaid 流程图 HTML

---

## 用法

```
$ARGUMENTS
```

`$ARGUMENTS` 是要生成流程图的描述文字、文件路径或模块名。

**示例：**
- `用户登录流程：输入账号密码 → 校验 → 成功跳转主页 / 失败提示重试`
- `assistant/perception/perception.py` — 读源码生成流程图
- `payment 退款流程` — 关键词+描述

---

## 执行步骤

### Step 1：理解输入

路径约定：所有相对路径基于项目根目录。

如果 `$ARGUMENTS` 看起来像文件/目录路径（含 `/` 或 `\` 或扩展名）：
- 读取目标文件（`.py` / `.md` / `.html`），提取流程逻辑
- 如果是目录，glob `**/*.py` 读取核心文件（≤15 个全部读，超出按文件名判断核心文件）

否则 `$ARGUMENTS` 就是流程描述文本：
- 优先搜索：在项目中搜索是否已有相关模块名或函数名，用于校准节点命名，了解确切的代码逻辑，对于画流程图很有帮助。搜不到也不影响生成。
- 如果搜索不到，那就直接使用用户的描述文案。直接按照 Step 3 直接画流程图即可。

如果 `$ARGUMENTS` 为空或不足以生成流程图，可以使用工具：`ask_user`，反问用户补充关键步骤。

---

### Step 2：确定输出路径

输出到 `docs/{name}/`，文件名从输入推断：
- 文件路径 → 取文件名去扩展名
- 描述文本 → 取前 2-3 个关键词

如: `docs/{name}/{name}_flowchart.html`

---

### Step 3：编写 Mermaid 流程图

#### 方法论：如何画好一个流程图

1. **识别起止点**：流程从哪开始（入口/触发）、到哪结束（成功/失败/返回），起点和终点必须明确
2. **提取关键决策点**：菱形节点 `{判断条件}`，只画影响后续路径的分支（校验、权限、状态机切换）
3. **控制粒度**：一个流程图 5~15 个节点最佳，超过 15 个用 subgraph 分层；不要画函数内部实现，只画模块间调用和关键决策
4. **确保每条路径闭环**：每个分支都有明确的下一步或终点，不能有悬空箭头
5. **分层原则**：只在有明确逻辑边界时用 subgraph（如"客户端→服务端→数据库"），不为了分层而分层
6. **少交叉原则**：流程图的线与线之间尽量少交叉（交叉可能意味着逻辑上不清晰，认真反思一下），如果实在避不开可以交叉。

#### 语法规范

- 图方向：默认 `flowchart TD`，横向数据流用 `flowchart LR`，节点多的时候可以混合使用
- 节点命名：`ID["显示文字"]`，ID 用简短大写英文
- 多行文字：`ID["第一行<br/>第二行"]`
- 子图：`subgraph NAME["标签"]` 包裹逻辑层
- 箭头：关键判断分支加 `-->|"说明"|`，普通流程不加
- 源码模式：类名、方法名严格取自已读到的源码，不虚构；描述模式：按描述生成，不额外发挥

#### 节点颜色体系（必须使用 classDef）

Mermaid 代码必须包含以下 5 类 classDef，HTML 模板的 `enhanceSvg` 会自动匹配渐变描边和文字颜色：

```mermaid
classDef main      fill:#1a1d27,stroke:#4f8ef7,stroke-width:2.5px
classDef branch    fill:#1e1b1a,stroke:#f97316,stroke-width:2.5px
classDef exception fill:#201a1c,stroke:#ef4444,stroke-width:2.5px
classDef decide    fill:#1c1a28,stroke:#7c3aed,stroke-width:2.5px
classDef success   fill:#161f1c,stroke:#22c55e,stroke-width:2.5px
```

| class | 语义 | fill | stroke |
|-------|------|------|--------|
| `main` | 核心流程/入口节点 | `#1a1d27` | `#4f8ef7` 蓝 |
| `branch` | 分支操作/编辑节点 | `#1e1b1a` | `#f97316` 橙 |
| `exception` | 异常/拒绝/失败 | `#201a1c` | `#ef4444` 红 |
| `decide` | 判断/菱形节点 | `#1c1a28` | `#7c3aed` 紫 |
| `success` | 成功/确认节点 | `#161f1c` | `#22c55e` 绿 |

**必须**用 `class NODE1,NODE2,... main` 给节点分配分类。

#### Subgraph 风格

每个 subgraph 加极淡背景：

```
style SUBGRAPH_ID fill:#13151c,stroke:none
```

#### 连线

```
linkStyle default stroke:#2e3352,stroke-width:1.5px
```

#### 字号要求

| 元素 | 字号 | 规则 |
|------|------|------|
| 子图标题 | **≤ 28px** | 不可超过 |
| 节点文字 | **≥ 30px** | 30px 起步 |
| 连线标签 | **≥ 30px** | 30px 起步 |

> **注意**：不要在 Mermaid 代码中设置 `fontSize`，字号由 HTML 模板的 `enhanceSvg` 统一控制。常规参数：节点 30px，子图标题 28px，连线标签 30px。

---

### Step 4：生成 HTML 文件

HTML 模板与规范在 `references/flowchart-style.md`。

读取该文件，取「一、HTML 页面模板」的完整 HTML 模板，替换：
- `{{标题}}` → 流程名称（标题使用渐变色，自动渲染为蓝紫橙渐变）
- `{{来源说明}}` → 输入来源简述
- `{{PHASE_BADGES}}` → 如果流程有 subgraph 分区，从 subgraph 标签名生成一组彩色徽章（如 `<span class="badge badge-purple">入口</span>`）；没有 subgraph 则替换为空字符串
- `{{MERMAID_CODE}}` → Step 3 生成的完整 Mermaid 代码（含 flowchart 定义 + 节点 + 连线 + classDef + class + style + linkStyle）

> 模板内置了 `enhanceSvg` 函数，会自动处理节点渐变描边、阴影、圆角、字号、subgraph 标题防截断和 viewBox 自适应。无需手动调整 SVG。

---

### Step 5：写入文件并报告

写入目标路径，报告：
- 输出文件路径
- 流程名称

---

## 模板与规范

全部在 `references/flowchart-style.md`：
- 一、HTML 页面模板（完整 HTML，直接替换占位符）
- 二、Mermaid 代码规范（颜色语义、节点写法、classDef 体系、子图风格）
