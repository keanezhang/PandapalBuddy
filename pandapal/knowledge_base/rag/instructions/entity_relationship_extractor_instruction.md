---
id: entity_relationship_extractor_instruction
version: v1.3
description: 实体和关系联合抽取器的Agent指令（支持人物、地名、组织、事件四种实体类型，针对修仙/玄幻小说优化；方案A 事件为节点）
---

从用户给定的文本中：中文分词，提取实体、关系、词元及关键词，并关联其所属子文档片段 id。

---

## 【提取优先级】

- **第一优先级（必须）**：实体、核心事件；
- **第二优先级（尽量）**：核心人物关系（师徒、从属）、主要地点关系、组织归属关系。
- **第三优先级（可选）**：次要关系、边缘信息。

---

## 【工作流程】
1. **理解全文**：用完整文本抽取，保持语义完整；识别隐晦表达与间接关系。
2. **提取实体**：类型限以下四种，必须正确标注 `label`。各字段含义与约束见【输出格式与约束】。
   {ENTITY_LABELS_SECTION}
3. **识别关系**：仅基于文本明确提及或可可靠推断，不脑补。人物↔人物、人物↔地点、人物↔组织、组织↔地点、人物↔事件、事件↔事件、事件↔地点。**遵守方向与类型**见「关系方向规则」；事件尽量挂 child_chunk_ids。关系字段约束见【输出格式与约束】。
4. **词元与关键词**：按 chunk 提取 segmented_words、keywords；BM25 仅使用 segmented_words 构建与检索，向量使用 keywords。格式与约束见【输出格式与约束】。
5. **chunk 归属**（若提供子文档信息）：根据 chunk_info_section 判断实体/关系所属子文档；一个实体或关系可属于多个子文档；编号从 1 开始。
6. **输出**：严格按【输出格式与约束】中的 JSON 格式，用 ```json … ``` 包裹。若提供子文档列表，须输出 lexical。

---

## 【关系类型定义】

关系类型必须为以下枚举之一（严禁 UNKNOWN 等）：

{RELATION_TYPES_SECTION}

---

## 【关系方向规则】必须遵守

有方向的关系，source/target 须按下列规则设置。

**人物之间**  
- MASTER: source=师父，target=弟子  
- SUBORDINATE: source=下级，target=上级  
- CREDITOR: source=债主，target=债务人 | DEBTOR: source=债务人，target=债主  
- MENTOR: source=指导者，target=被指导者  

**人物↔地点**  
- RESIDES_IN/BORN_IN/ACTIVITY_AT/PASSES_THROUGH: source=人物，target=地点（RESIDES_IN 仅长期居住/闭关；勿把活动/途经标成居住）  
- RULES/GUARDIAN_OF: source=人物，target=地点  

**人物↔组织**  
- BELONGS_TO/FOUNDED/LEADER_OF: source=人物，target=组织  
- LEFT/BETRAYED: source=人物，target=组织  

**事件相关**  
- PARTICIPATES_IN: source=人物或组织，target=事件  
- TRIGGERS: source=人物或事件，target=事件  
- CAUSES: source=事件，target=事件  
- OCCURS_AT: source=事件，target=地点  
- NARRATIVE_FOCUS: source=事件，target=人物（叙事视角）  
- SELECTED_BY: source=事件，target=人物  

无方向类型（FRIEND、ENEMY、FAMILY、SPOUSE、SCHOOLFELLOW、ALLY、RIVAL、LOVER、NEIGHBOR）方向可任设，建议一致。

---

## 【输出格式与约束】

- **Token 上限**：{DEFAULT_MAX_TOKENS_JOINT}。超限时优先保证 entities、relationships 完整，lexical 可适当精简。
- **格式**：仅输出以下 JSON，用 markdown 代码块包裹。

```json
{
  "entities": [
    {
      "name": "实体标准名称",
      "label": "PER",
      "aliases": ["别名1", "别名2"],
      "description": "简要描述",
      "frequency": 1,
      "source_texts": ["原文片段1", "原文片段2"],
      "event_type": null,
      "child_chunk_ids": ["parent_chapter_1_child_0"]
    }
  ],
  "relationships": [
    {
      "source": "实体1标准名称",
      "target": "实体2标准名称",
      "relation_type": "关系类型枚举值",
      "description": "关系说明",
      "confidence": 0.85,
      "source_text": ["原文片段"],
      "child_chunk_ids": ["parent_chapter_1_child_0"]
    }
  ],
  "lexical": [
    {
      "child_chunk_id": "parent_chapter_1_child_0",
      "segmented_words": ["词元1", "词元2"],
      "keywords": ["关键词1", "关键词2"]
    }
  ]
}
```

**字段要点**（此处为实体、关系、lexical 的**唯一权威定义**，工作流程中不再重复）

- **entities**：
 - label 必为 PER/LOC/ORG/EVENT 之一；
 - name 唯一，用简短名称（人物/组织/地点用最正式常用名；事件用 4–8 字动词短语，公式：动词+宾语，如「获得本命字」「救宁姚」「离开小镇」）；
 - event_type 仅事件使用（核心事件/际遇/转折事件等，事件必提情节）；
 - aliases 为每个实体收集代表性标准名、所有别名（道号/法号/尊号/简称等，事件可无别名）；
 - description 为总结性简单描述；
 - source_texts 为每个实体收集代表性、相关原文片段，单条小于 100 字；
 - child_chunk_ids 必须从输入的子文档 id 中挑选。

- **relationships**：
 - source、target 必须在 entities 中存在；
 - relation_type 为上列枚举；confidence 0.0–0.95。
 - source_text 100字以内，**每条关系的 source_text 必须是「支撑该条关系的原文片段」**，且**不同关系应尽量对应不同片段**；禁止为多条关系填写完全相同的 source_text，除非同一段原文确实同时描述多件事（如同一段既写救人又写被选中）。
 - child_chunk_ids 关系所在的子文档那个id，事件尽量挂子文档id。

- **lexical**（segmented_words 供 BM25 构建与检索，keywords 供向量）：
- child_chunk_id 与输入一致。
- **segmented_words**：词元级中文分词，每元素为独立词、非短语，过滤停用词/语气词/纯标点，禁止非中文；BM25 仅使用此字段。
- **keywords**：过滤后中文关键词，含命名实体、专有名词、关键动作/事件，禁止非中文；向量使用。

**常见错误避免**  
| 错误 | 正确做法 |
|------|----------|
| 事件名过长 | 4–8 字动词短语，如 ✅「获得本命字」❌「齐静春赠予陈平安本命字静」 |
| 关系方向错 | 查「关系方向规则」：MASTER 师→徒，RESIDES_IN 人→地，PARTICIPATES_IN 人→事件 |
| 遗漏事件 | 关键情节（赠物、救人、被选中、命运转折）须转为 EVENT 并建 PARTICIPATES_IN/NARRATIVE_FOCUS |
| **多条关系共用同一 source_text** | 每条关系的 source_text 应取自「直接支撑该条关系」的原文；不同事件（如被齐静春选中、救宁姚、获得本命字）应对应不同段落，勿把同一段原文复制给多条关系。 |

**重要约束**  
1. 基于文本，关系须有原文依据、不脑补。
2. 每个实体 name 唯一，且不与其它实体的 aliases 重复。  
3. 关系的方向正确，严格按「关系方向规则」设置 source/target。
4. 仅输出 entities、relationships、lexical 三字段；无实体或关系时，entities、relationships 分别返回 `[]`。
5. 每个实体或关系的单条source_text **必须100字以内**。

**输出前自检清单**  
① 关键情节是否已转为事件实体？② 主要人物是否有 PARTICIPATES_IN？③ 重要事件是否有 NARRATIVE_FOCUS？④ 关系方向是否正确？⑤ child_chunk_ids 是否与输入一致？⑥ **不同关系是否尽量使用不同 source_text 片段**（勿把同一段原文复制给多条关系）？⑦ 总token数是否超过{DEFAULT_MAX_TOKENS_JOINT}？

**Reflexion（自检与修正）**  
输出前按上述自检清单逐项检查；若发现遗漏事件、关系方向错误、事件名过长或格式问题，请先修正再输出最终结果。确保最终结果通过自检后再提交。

请严格按以上流程与格式执行。
