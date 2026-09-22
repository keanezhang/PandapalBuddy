#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于 LLM 的查询意图分类器（图谱检索用）

一次输出：意图 + 实体列表 + 关系类型 + 策略 + 关键词 + 中文分词（segmented_words），
供 GraphStrategyExecutor 执行图谱检索，segmented_words 供 BM25 检索使用。
"""

import importlib.resources
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...rag_protocol import RAGLLMProvider
from ...instructions.instruction_loader import load_instruction
from ... import instructions as _instructions_pkg  # 模块对象引用，零包名耦合
from ...schema import QueryAnalysis, SchemaKeys, create_empty_query_analysis
from ...store.graph.relation_type import get_relation_type_display_label, match_relation_type_by_keyword
from ...store.graph.graph_constants import STRATEGY_TYPES

import logging

logger = logging.getLogger(__name__)

__all__ = ["QueryIntentClassifier"]


# =============================================================================
# 一、运行配置与路径（仅本模块使用）
# =============================================================================

DEFAULT_MAX_TOKENS_INTENT: int = 3000  # 意图分类默认最大 token（输出完整 JSON）
AGENT_NAME_QUERY_INTENT_CLASSIFIER: str = "query_intent_classifier"
MIN_ENTITY_LENGTH: int = 2  # 实体过滤：长度低于此值的实体将被丢弃


def _get_instructions_dir() -> Path:
    """通过 importlib.resources + 模块对象定位 instructions 包，不硬编码包名。"""
    ref = importlib.resources.files(_instructions_pkg)
    return Path(str(ref))


_INSTRUCTIONS_DIR = _get_instructions_dir()
_INSTRUCTION_ID = "query_intent_classifier"
_INSTRUCTION_FILENAME = "query_intent_classifier_instruction.md"


# =============================================================================
# 二、意图 / 关系 / 策略 / 推理 封闭取值（与 schema、graph_constants、reasoning 约定一致）
# =============================================================================

# 意图类型列表（7 类），取值与 SchemaKeys 一致，供校验与兜底
INTENT_TYPES: List[str] = [
    SchemaKeys.INTENT_FACTUAL,
    SchemaKeys.INTENT_RELATIONAL,
    SchemaKeys.INTENT_PLOT,
    SchemaKeys.INTENT_COMPARATIVE,
    SchemaKeys.INTENT_SUMMARY,
    SchemaKeys.INTENT_NAVIGATION,
    SchemaKeys.INTENT_LIST,
]

# 关系类型分组（仅本模块指令占位 RELATION_TYPES_TABLE 用，与 schema.RelationType 一致）
_RELATION_GROUP_PERSON_PERSON: Tuple[str, ...] = (
    "MASTER", "FRIEND", "ENEMY", "FAMILY", "SPOUSE", "SCHOOLFELLOW",
    "SUBORDINATE", "ALLY", "MENTOR", "RIVAL", "LOVER", "NEIGHBOR", "CREDITOR", "DEBTOR",
)
_RELATION_GROUP_PERSON_LOC: Tuple[str, ...] = (
    "RESIDES_IN", "BORN_IN", "ACTIVITY_AT", "PASSES_THROUGH", "RULES", "GUARDIAN_OF",
)
_RELATION_GROUP_PERSON_ORG: Tuple[str, ...] = (
    "BELONGS_TO", "FOUNDED", "LEADER_OF", "LEFT", "BETRAYED",
)
_RELATION_GROUP_EVENT: Tuple[str, ...] = (
    "PARTICIPATES_IN", "TRIGGERS", "CAUSES", "OCCURS_AT", "NARRATIVE_FOCUS", "SELECTED_BY",
)
RELATION_GROUPS: List[Tuple[str, Tuple[str, ...]]] = [
    ("【人物之间关系类型】", _RELATION_GROUP_PERSON_PERSON),
    ("【人物与地点关系类型（专属，须严格区分）】", _RELATION_GROUP_PERSON_LOC),
    ("【人物与组织关系类型（专属）】", _RELATION_GROUP_PERSON_ORG),
    ("【事件相关关系类型（方案A：事件为节点 EVENT）】", _RELATION_GROUP_EVENT),
]

# 推理类型列表（仅本模块校验 reasoning_type 用）
REASONING_TYPES: List[str] = ["compare", "motivation", "hypothetical", "summary", "narrative"]


# =============================================================================
# 三、指令占位符文案（与 query_intent_classifier_instruction.md 中占位符一致）
# =============================================================================

_INTENT_TYPES_LIST = """- 事实型查询：问是谁、做什么、住哪等具体事实
- 关系型查询：问两人/多人之间是什么关系、谁的师傅是谁等
- 情节型查询：问发生了什么、过程、因果
- 比较型查询：问谁更…、对比
- 总结型查询：问某类关系有哪些、前几章概括等
- 导航型查询：问第几章、哪一节
- 列表型查询：问和X有关系的人有哪些、X的徒弟有谁等列表"""

_STRATEGY_TYPES_LIST = """- **entity_attribute**：查实体属性/描述（是谁、做什么）；情节型/比较型/导航型时表示图侧兜底。适合：事实型单实体；情节/比较/总结/导航无专有策略时兜底。
- **direct_relation**：查一度直接关系（X的师傅是谁、A和B什么关系、A和B有什么故事关联/交集）。适合：1~2 实体 + 关系型；双实体问「两人之间的关联」时必选，保证召回双实体共现片段。
- **path_query**：查两实体间路径。适合：明确两个实体且问「有什么关系/怎么联系」。
- **graph_structure**：查社区/某类关系全集。适合：总结型如「师徒关系有哪些」。
- **common_relation_endpoint**：查多实体在某一关系下的共同端（如共同的师傅是谁）。适合：问句含「共同」「同一个」+ 多实体 + 关系类型词。
- **list_by_relation_type**：按关系类型列全集。适合：「X关系有哪些」。
- **list_by_entity**：查某实体的关系网络/邻居。适合：「和X有关系的人有哪些」「X和谁有交集」。
- **expand_entity**：以实体为中心多跳邻居相关 chunk（2 跳）。适合：情节型/总结型/列表型要更全背景（可与 entity_attribute 多策略组合）。
- **narrative_events**：叙事事件（事件相关策略，按实体查其参与的核心事件、际遇、叙事焦点）。适合：情节型/总结型/角色定位类；可与 entity_attribute、expand_entity 组合。"""

_REASONING_TYPES_LIST = """- **compare**：对比分析（谁更…、本质区别、与…有何不同）
- **motivation**：动机/意图推断（为什么、至少几层动机、从表象到深层）
- **hypothetical**：反事实假设（如果…那么…、若不…则…）
- **summary**：支线/关系梳理与归纳（梳理某条线、关系变化、分别影响了谁）
- **narrative**：情节/过程与影响（发生了什么、过程怎样、对决策/状态的影响）"""


# =============================================================================
# 四、指令变量构建（供 Agent 加载 .md 时注入占位符）
# =============================================================================

def _build_intent_classifier_instruction_variables() -> Dict[str, Any]:
    """返回意图分类器指令 .md 的占位变量（关系表按 RELATION_GROUPS 拼出，其余三段用本模块常量）。"""
    all_rt = set()
    for _, type_list in RELATION_GROUPS:
        all_rt.update(type_list)
    relation_table = "\n".join(f"| {rt} | {get_relation_type_display_label(rt)} |" for rt in sorted(all_rt))
    return {
        "RELATION_TYPES_TABLE": relation_table,
        "INTENT_TYPES_LIST": _INTENT_TYPES_LIST,
        "STRATEGY_TYPES_LIST": _STRATEGY_TYPES_LIST,
        "REASONING_TYPES_LIST": _REASONING_TYPES_LIST,
    }


# =============================================================================
# 五、查询意图分类器（LLM 调用与结果解析）
# =============================================================================

class QueryIntentClassifier:
    """
    基于 LLM 的查询意图分类器（图谱检索用）

    一次输出：intent, entities（字符串列表）, relation_type, strategies（策略列表）, keywords, segmented_words（中文分词），
    供 search_graph → GraphStrategyExecutor 执行图谱检索；segmented_words 供 BM25 检索使用。
    """

    def __init__(
        self,
        llm_provider: Optional[RAGLLMProvider] = None,
        verbose: bool = True,
        instructions: Optional[str] = None,
    ):
        """
        Args:
            llm_provider: 用于 LLM 调用的 RAGLLMProvider 实现（可选，无则返回空查询分析）。
            verbose: 是否输出详细日志。
            instructions: 外部传入的指令文本（可选）。传入则直接使用，不传则从内置 .md 加载。
        """
        self.verbose = verbose
        self.llm_provider = llm_provider
        if llm_provider is not None:
            self._agent_instructions = instructions if instructions else self._build_agent_instructions()
            if self.verbose:
                source = "外部传入" if instructions else "内置 .md"
                logger.info("QueryIntentClassifier 使用注入的 llm_provider，指令来源: %s", source)
        else:
            self._agent_instructions = ""
            if self.verbose:
                logger.info("QueryIntentClassifier 无 llm_provider，将返回空查询分析")

    def get_instruction(self) -> str:
        """返回当前加载的指令全文（变量已替换），便于审计或保存到 using_prompt。"""
        return getattr(self, "_agent_instructions", "") or ""

    def _build_agent_instructions(self) -> str:
        """构建 Agent 指令：从本模块变量拼占位符，加载 .md 并注入。"""
        variables = _build_intent_classifier_instruction_variables()
        instruction_file = _INSTRUCTIONS_DIR / _INSTRUCTION_FILENAME
        return load_instruction(instruction_file, variables=variables)

    async def classify_intent(self, query: str) -> Optional[QueryAnalysis]:
        """识别意图并输出 intent, entities, relation_type, strategies, keywords, segmented_words。"""
        if self.llm_provider is None:
            if self.verbose:
                logger.debug(" 无 llm_provider，返回空查询分析")
            return create_empty_query_analysis()
        if not query or not query.strip():
            if self.verbose:
                logger.warning(" 查询为空，无法进行意图分类")
            return None

        parsed = await self._call_llm_and_parse(query)
        if parsed is None:
            return None

        return self._validate_and_normalize(parsed)

    async def _call_llm_and_parse(self, query: str) -> Optional[Dict[str, Any]]:
        """调用 LLM 获取意图分类结果并解析为 JSON dict。"""
        prompt = f'用户问题："{query}"'
        content: Optional[str] = None
        try:
            content = await self.llm_provider.run(
                prompt,
                self._agent_instructions,
                agent_name=AGENT_NAME_QUERY_INTENT_CLASSIFIER,
                max_tokens=DEFAULT_MAX_TOKENS_INTENT,
            )
            content = (content or "").strip()
        except Exception as e:
            if self.verbose:
                logger.warning(" 意图分类 LLM 调用失败: %s", e)
            return None

        content_cleaned = content.strip()
        code_block = re.search(r"```(?:json)?\s*(.*?)\s*```", content_cleaned, re.DOTALL)
        if code_block:
            inner = code_block.group(1).strip()
            json_str = self._extract_json_object(inner) or inner
        else:
            json_str = self._extract_json_object(content_cleaned)
        if not json_str:
            if self.verbose:
                logger.warning(" 未解析到 JSON")
            return None

        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            if self.verbose:
                logger.warning(" JSON 解析失败: %s", e)
            return None

    @staticmethod
    def _parse_bool(value: Any) -> bool:
        """将布尔值或字符串解析为 bool。"""
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"):
            return True
        return False

    def _validate_and_normalize(self, parsed: Dict[str, Any]) -> QueryAnalysis:
        """从 LLM 解析结果中提取、校验和规范化各字段。"""
        K = SchemaKeys
        intent = (parsed.get(K.QA_INTENT) or "").strip()
        entities_raw = parsed.get(K.QA_ENTITIES, [])
        if not isinstance(entities_raw, list):
            entities_raw = []
        entities = [str(x).strip() for x in entities_raw if x and len(str(x).strip()) >= MIN_ENTITY_LENGTH]

        relation_type = parsed.get(K.QA_RELATION_TYPE)
        if relation_type is not None and relation_type != "null":
            relation_type = str(relation_type).strip() or None
            if relation_type:
                mapped = match_relation_type_by_keyword(relation_type, verbose=False)
                relation_type = mapped if mapped else None
        else:
            relation_type = None

        strategies_raw = parsed.get(K.QA_STRATEGIES, [])
        if isinstance(strategies_raw, list) and len(strategies_raw) > 0:
            strategies = [str(s).strip() for s in strategies_raw if s and str(s).strip()]
            strategies = [s for s in strategies if s in STRATEGY_TYPES]
        else:
            strategies = []
        if not strategies:
            strategies = [STRATEGY_TYPES[0]]
        keywords = parsed.get(K.QA_KEYWORDS, [])
        if not isinstance(keywords, list):
            keywords = []
        keywords = [str(k).strip() for k in keywords if k]

        segmented_words = parsed.get(K.QA_SEGMENTED_WORDS, [])
        if not isinstance(segmented_words, list):
            segmented_words = []
        segmented_words = [str(w).strip() for w in segmented_words if w]

        need_reasoning = self._parse_bool(parsed.get(K.QA_NEED_REASONING))

        reasoning_type = parsed.get(K.QA_REASONING_TYPE)
        if reasoning_type is None or reasoning_type == "null" or (isinstance(reasoning_type, str) and not reasoning_type.strip()):
            reasoning_type = None
        else:
            reasoning_type = str(reasoning_type).strip().lower()
            if reasoning_type not in REASONING_TYPES:
                reasoning_type = REASONING_TYPES[-1] if need_reasoning else None

        if intent not in INTENT_TYPES:
            for t in INTENT_TYPES:
                if t.strip() == intent or (intent and intent in t):
                    intent = t
                    break
            else:
                intent = INTENT_TYPES[0]

        out: QueryAnalysis = {
            K.QA_INTENT: intent,
            K.QA_ENTITIES: entities,
            K.QA_RELATION_TYPE: relation_type,
            K.QA_STRATEGIES: strategies,
            K.QA_KEYWORDS: keywords,
            K.QA_SEGMENTED_WORDS: segmented_words,
            K.QA_NEED_REASONING: need_reasoning,
            K.QA_REASONING_TYPE: reasoning_type,
        }
        if self.verbose:
            logger.info(
                "   intent=%s, strategies=%s, entities=%s, relation_type=%s, need_reasoning=%s, reasoning_type=%s",
                intent, strategies, entities, relation_type, need_reasoning, reasoning_type,
            )
        return out

    # ---------- 工具方法：从 LLM 输出中抽取 JSON 对象 ----------
    @staticmethod
    def _extract_json_object(text: str) -> Optional[str]:
        start = text.find("{")
        if start == -1:
            return None
        brace = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                brace += 1
            elif text[i] == "}":
                brace -= 1
                if brace == 0:
                    return text[start : i + 1]
        return None
