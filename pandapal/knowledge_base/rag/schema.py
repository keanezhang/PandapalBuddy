#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG 纯数据结构（schema）

原则：纯数据结构统一放在本文件。含字段名（SchemaKeys）、TypedDict/dataclass、
create_empty_*、validate_*。管理函数按流程分在其它文件，仅引用本 schema。

- schema.py（本文件）：SchemaKeys（含意图类型常量）、Entity、Relationship、QueryAnalysis、ChildDocument、
  ParentDocument、RawRetrievalResult、FusionConfig、ChunkLexicalInfo、ExtractionResult；create_empty_*、validate_*。
  RelationType 在本文件单点定义（Relationship.relation_type 字段类型）；graph.relation_type 从本模块引用并扩展关键词/展示等。
- search.constants：匹配类型、召回源；意图类型列表在 query_intent_classifier；reasoning/graph 子域各有 constants（推理/策略与实体）。
"""

from typing import Final, List, Dict, Any, Optional, TypedDict, Union
from types import MappingProxyType
from dataclasses import dataclass, field
from enum import Enum

from .exceptions import RAGConfigError

# ==================== 0. 字段名常量（集中管理，单点修改） ====================

class SchemaKeys:
    """
    RAG 全流程用到的字典键名常量。所有 .get(key)、[key] 应使用本类常量，避免魔法字符串。
    新增字段时：在此添加常量，并在对应 TypedDict、create_empty_*、validate_* 中使用。
    """
    # ----- QueryAnalysis -----
    QA_INTENT: Final = "intent"
    QA_ENTITIES: Final = "entities"
    QA_STRATEGIES: Final = "strategies"
    QA_RELATION_TYPE: Final = "relation_type"
    QA_KEYWORDS: Final = "keywords"
    QA_SEGMENTED_WORDS: Final = "segmented_words"
    QA_EXPANDED_ENTITIES: Final = "expanded_entities"
    QA_STANDARD_ENTITY_NAMES: Final = "standard_entity_names"
    QA_NEED_REASONING: Final = "need_reasoning"
    QA_REASONING_TYPE: Final = "reasoning_type"
    # understanding 传给 GraphStrategyExecutor 时复用上述键，另加单策略（循环时写入）
    QA_STRATEGY: Final = "strategy"  # 单策略，与 QA_STRATEGIES 区分

    # ----- RawRetrievalResult -----
    RR_DOCUMENTS: Final = "documents"
    RR_METADATAS: Final = "metadatas"
    RR_DISTANCES: Final = "distances"
    RR_IDS: Final = "ids"

    # ----- ChildDocumentMetadata -----
    CDM_CHILD_CHUNK_ID: Final = "child_chunk_id"
    CDM_PARENT_ID: Final = "parent_id"
    CDM_CHILD_CHUNK_INDEX: Final = "child_chunk_index"
    CDM_TOTAL_CHILD_CHUNKS: Final = "total_child_chunks"
    CDM_SOURCE: Final = "source"
    CDM_CHAPTER_INDEX: Final = "chapter_index"
    CDM_CHAPTER_TITLE: Final = "chapter_title"
    CDM_SCENE_INDEX: Final = "scene_index"
    CDM_SCENE_TITLE: Final = "scene_title"
    CDM_KEYWORDS: Final = "keywords"
    CDM_SEGMENTED_WORDS: Final = "segmented_words"
    CDM_MATCH_TYPE: Final = "match_type"
    CDM_MATCHED_KEYWORDS: Final = "matched_keywords"

    # ----- ChildDocument -----
    CD_CONTENT: Final = "content"
    CD_METADATA: Final = "metadata"
    CD_DISTANCE: Final = "distance"
    CD_CHILD_ID: Final = "child_id"
    CD_PARENT_ID: Final = "parent_id"
    CD_MATCH_TYPE: Final = "match_type"
    CD_RETRIEVAL_SOURCES: Final = "retrieval_sources"
    CD_GRAPH_HIT: Final = "graph_hit"
    CD_GRAPH_SCORE: Final = "graph_score"  # 图谱统一相关性总分（0~1），用于排序/融合
    CD_BM25_HIT: Final = "bm25_hit"
    CD_RERANK_SCORE: Final = "rerank_score"
    CD_RRF_SCORE: Final = "rrf_score"
    CD_FINAL_SCORE: Final = "final_score"

    # ----- ParentDocumentMetadata -----
    PDM_PARENT_ID: Final = "parent_id"
    PDM_TITLE: Final = "title"
    PDM_SOURCE: Final = "source"
    PDM_CHAPTER_INDEX: Final = "chapter_index"
    PDM_CHAPTER_TITLE: Final = "chapter_title"
    PDM_SCENE_INDEX: Final = "scene_index"
    PDM_SCENE_TITLE: Final = "scene_title"

    # ----- ParentDocument -----
    PD_PARENT_ID: Final = "parent_id"
    PD_PARENT_CONTENT: Final = "parent_content"
    PD_PARENT_METADATA: Final = "parent_metadata"
    PD_MATCHED_CHILDREN: Final = "matched_children"
    PD_MATCHED_CHILDREN_COUNT: Final = "matched_children_count"
    PD_BEST_DISTANCE: Final = "best_distance"
    PD_RELEVANCE_SCORE: Final = "relevance_score"
    PD_RERANK_SCORE: Final = "rerank_score"
    PD_RETRIEVAL_SOURCES: Final = "retrieval_sources"
    PD_GRAPH_HIT: Final = "graph_hit"
    PD_BM25_HIT: Final = "bm25_hit"
    PD_RRF_SCORE: Final = "rrf_score"

    # ----- Entity/Relationship 常用（LLM 输出解析等） -----
    ENT_TEXT: Final = "text"  # 兼容 entities 中 { "text": "xxx" }
    ENT_NAME: Final = "name"
    ENT_ALIASES: Final = "aliases"
    ENT_LABEL: Final = "label"
    ENT_EVENT_TYPE: Final = "event_type"  # 仅 EVENT 实体使用（核心事件/际遇/转折事件等）
    ENT_CHILD_CHUNK_IDS: Final = "child_chunk_ids"
    REL_SOURCE: Final = "source"
    REL_TARGET: Final = "target"
    REL_RELATION_TYPE: Final = "relation_type"
    REL_SOURCE_CHUNK: Final = "source_chunk"

    # ----- 抽取输出顶层键（LLM 联合抽取 entities/relationships/lexical） -----
    EXTRACTION_ENTITIES: Final = "entities"
    EXTRACTION_RELATIONSHIPS: Final = "relationships"
    EXTRACTION_LEXICAL: Final = "lexical"

    # ----- 意图类型常量（跨子域共享：search + reasoning 均使用） -----
    INTENT_FACTUAL: Final = "factual"
    INTENT_RELATIONAL: Final = "relational"
    INTENT_PLOT: Final = "plot"
    INTENT_COMPARATIVE: Final = "comparative"
    INTENT_SUMMARY: Final = "summary"
    INTENT_NAVIGATION: Final = "navigation"
    INTENT_LIST: Final = "list"

# Relationship 默认置信度（本结构用，不依赖 graph）
DEFAULT_CONFIDENCE: float = 0.8

# ==================== 1. 图谱数据（构建/存储） ====================


class RelationType(str, Enum):
    """关系类型枚举：知识图谱中实体之间的关系类型。"""
    MASTER = "MASTER"
    FRIEND = "FRIEND"
    ENEMY = "ENEMY"
    FAMILY = "FAMILY"
    SPOUSE = "SPOUSE"
    SCHOOLFELLOW = "SCHOOLFELLOW"
    SUBORDINATE = "SUBORDINATE"
    ALLY = "ALLY"
    MENTOR = "MENTOR"
    RIVAL = "RIVAL"
    LOVER = "LOVER"
    NEIGHBOR = "NEIGHBOR"
    CREDITOR = "CREDITOR"
    DEBTOR = "DEBTOR"
    RESIDES_IN = "RESIDES_IN"
    BORN_IN = "BORN_IN"
    ACTIVITY_AT = "ACTIVITY_AT"
    PASSES_THROUGH = "PASSES_THROUGH"
    RULES = "RULES"
    GUARDIAN_OF = "GUARDIAN_OF"
    BELONGS_TO = "BELONGS_TO"
    FOUNDED = "FOUNDED"
    LEADER_OF = "LEADER_OF"
    LEFT = "LEFT"
    BETRAYED = "BETRAYED"
    PARTICIPATES_IN = "PARTICIPATES_IN"
    TRIGGERS = "TRIGGERS"
    CAUSES = "CAUSES"
    OCCURS_AT = "OCCURS_AT"
    NARRATIVE_FOCUS = "NARRATIVE_FOCUS"
    SELECTED_BY = "SELECTED_BY"
    UNKNOWN = "UNKNOWN"


@dataclass
class Entity:
    """
    实体数据结构
    
    用于表示知识图谱中的实体（人物、地点、组织等）。
    
    使用场景：在数据创建阶段，从文档中提取实体并存储到图谱中。
    
    字段说明：
    - name: 实体的标准名称（必需，用于唯一标识）
    - aliases: 别名列表（用于支持多种称呼方式）
    - label: 实体类型（PER/LOC/ORG/EVENT 等）
    - event_type: 事件子类型（仅 EVENT 使用，如核心事件/际遇/转折事件等）
    - description: 实体描述
    - frequency: 出现频率
    - first_appearance: 首次出现的文档ID
    - first_book_title: 首次出现的书名
    - first_chapter_index: 首次出现的章节索引
    - first_chapter_title: 首次出现的章节标题
    - first_scene_index: 首次出现的场景索引
    - first_scene_title: 首次出现的场景标题
    - source_texts: 来源文本片段列表
    - metadata: 其他元数据（可扩展字段）
    """
    name: str  # 标准名称
    aliases: List[str] = field(default_factory=list)  # 别名列表
    label: str = "PER"  # 实体类型（PER/LOC/ORG/EVENT 等）
    event_type: Optional[str] = None  # 事件子类型（仅 EVENT 使用，如核心事件/际遇/转折事件等）
    description: Optional[str] = None  # 描述
    frequency: int = 1  # 出现频率
    first_appearance: Optional[str] = None  # 首次出现的文档ID
    first_book_title: Optional[str] = None  # 首次出现的书名
    first_chapter_index: Optional[int] = None  # 首次出现的章节索引
    first_chapter_title: Optional[str] = None  # 首次出现的章节标题
    first_scene_index: Optional[int] = None  # 首次出现的场景索引
    first_scene_title: Optional[str] = None  # 首次出现的场景标题
    source_texts: List[str] = field(default_factory=list)  # 来源文本片段
    metadata: Dict[str, Any] = field(default_factory=dict)  # 其他元数据

    def __hash__(self):
        """实体哈希值（基于名称，不区分大小写）"""
        return hash((self.name.lower(), self.label))

    def __eq__(self, other):
        """实体相等性判断（基于名称和标签，不区分大小写）"""
        if not isinstance(other, Entity):
            return False
        return self.name.lower() == other.name.lower() and self.label == other.label


@dataclass
class Relationship:
    """
    关系数据结构
    
    用于表示知识图谱中实体之间的关系。
    
    使用场景：在数据创建阶段，从文档中提取实体间的关系并存储到图谱中。
    
    字段说明：
    - source: 源实体名称（必需）
    - target: 目标实体名称（必需）
    - relation_type: 关系类型（必需，使用RelationType枚举）
    - description: 关系描述
    - source_text: 来源文本（关系提取的原始文本）
    - confidence: 置信度（0-1之间）
    - source_doc: 来源文档ID
    - source_chunk: 来源文档块ID（子文档ID格式：parent_chapter_1_child_0）
    - book_title: 书名
    - chapter_index: 章节索引
    - chapter_title: 章节标题
    - scene_index: 场景索引
    - scene_title: 场景标题
    - metadata: 其他元数据（可扩展字段）
    """
    source: str  # 源实体名称
    target: str  # 目标实体名称
    relation_type: RelationType  # 关系类型
    description: Optional[str] = None  # 关系描述
    source_text: str = ""  # 来源文本
    confidence: float = DEFAULT_CONFIDENCE  # 置信度（0-1）
    source_doc: Optional[str] = None  # 来源文档ID
    source_chunk: Optional[str] = None  # 来源文档块ID（子文档ID格式）
    book_title: Optional[str] = None  # 书名
    chapter_index: Optional[int] = None  # 章节索引
    chapter_title: Optional[str] = None  # 章节标题
    scene_index: Optional[int] = None  # 场景索引
    scene_title: Optional[str] = None  # 场景标题
    metadata: Dict[str, Any] = field(default_factory=dict)  # 其他元数据

    def __hash__(self):
        """关系哈希值（基于源实体、目标实体和关系类型，不区分大小写）"""
        return hash((self.source.lower(), self.target.lower(), self.relation_type.value))

    def __eq__(self, other):
        """关系相等性判断（基于源实体、目标实体和关系类型，不区分大小写）"""
        if not isinstance(other, Relationship):
            return False
        return (
            self.source.lower() == other.source.lower() and
            self.target.lower() == other.target.lower() and
            self.relation_type == other.relation_type
        )


# ==================== 1.1 构建层：子文档词法信息 ====================
# 与 ChildDocumentMetadata 的 child_chunk_id/segmented_words/keywords 一致，供抽取器、LexicalEnricher、向量/BM25 构建使用

@dataclass
class ChunkLexicalInfo:
    """
    子文档词法分析结果（向量使用 keywords；BM25 仅使用 segmented_words 构建索引与检索）。

    字段与 SchemaKeys.CDM_CHILD_CHUNK_ID / CDM_SEGMENTED_WORDS / CDM_KEYWORDS 对应。
    由 EntityRelationshipExtractorLLM 产出 lexical，经 LexicalEnricher 写入 chunk.metadata。
    """
    child_chunk_id: str
    segmented_words: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)


@dataclass
class ExtractionResult:
    """
    联合抽取结果：实体、关系 +（可选）子文档词法分析。

    由 EntityRelationshipExtractorLLM 返回，LexicalEnricher 等消费。
    字段与 SchemaKeys.EXTRACTION_ENTITIES / EXTRACTION_RELATIONSHIPS / EXTRACTION_LEXICAL 对应。
    """
    entities: List[Entity] = field(default_factory=list)
    relationships: List[Relationship] = field(default_factory=list)
    lexical: Dict[str, ChunkLexicalInfo] = field(default_factory=dict)
    error: Optional[str] = None


# ==================== 2. 查询分析（检索入口） ====================

class QueryAnalysis(TypedDict, total=False):
    """
    查询分析结果数据结构（图谱检索用：意图 + 策略 + 实体）

    使用场景：在查询分析阶段，对用户查询进行意图识别、实体提取、策略选择。
    结果用于 GraphStrategyExecutor 执行图谱检索。

    字段说明：
    - intent: 意图类型（7 选 1，见 SchemaKeys 的 INTENT_* 常量或 query_intent_classifier.INTENT_TYPES）
    - entities: 实体名称字符串列表（用于图谱查询）
    - strategies: 图谱策略键列表（必填，至少一个；多策略时依次执行并合并 child_ids）
    - relation_type: 关系类型枚举（MASTER / FRIEND 等）或 None
    - keywords: 关键词列表
    - segmented_words: 中文分词结果（由 QueryIntentClassifier 输出，用于 BM25 检索；可由上层富化补充）
    - expanded_entities: 【上层统一富化】扩展实体列表（标准名+别名）
    - standard_entity_names: 【上层统一富化】标准名列表，供图谱关系查询使用
    - need_reasoning: 是否需要深度推理（证据+结构化提示→LLM），由意图分类器或 reasoning_router 打标
    - reasoning_type: 推理类型（compare/motivation/hypothetical/summary/narrative），仅 need_reasoning 为 True 时有效
    """
    intent: str  # 意图类型，默认为空字符串表示无意图
    entities: List[str]  # 实体名称字符串列表（图谱用）
    strategies: List[str]  # 图谱策略键列表（必填，至少一个；多策略时依次执行并合并 child_ids）
    relation_type: Optional[str]  # 关系类型枚举或 None
    keywords: List[str]  # 关键词列表
    segmented_words: List[str]  # 分词结果（用于 BM25 等）
    expanded_entities: List[str]  # 扩展实体（标准名+别名），由上层统一富化后填入
    standard_entity_names: List[str]  # 标准名列表，由上层统一富化后填入
    need_reasoning: bool  # 是否需要深度推理
    reasoning_type: Optional[str]  # 推理类型（compare/motivation/hypothetical/summary/narrative）


# ==================== 3. 检索中间/结果 ====================

class RawRetrievalResult(TypedDict, total=False):
    """
    原始检索结果数据结构（ChromaDB/BM25返回的格式）
    
    使用场景：在检索阶段，从存储系统（ChromaDB、BM25等）返回的原始格式。
    需要转换为标准格式（ChildDocument）。
    
    字段说明：
    - documents: 文档内容列表（嵌套列表格式：List[List[str]]）
    - metadatas: 元数据列表（嵌套列表格式：List[List[Dict]]）
    - distances: 距离列表（嵌套列表格式：List[List[float]]）
    - ids: 文档ID列表（嵌套列表格式：List[List[str]]）
    """
    documents: List[List[str]]  # 文档内容（嵌套列表，第一层是查询，第二层是结果）
    metadatas: List[List[Dict[str, Any]]]  # 元数据（嵌套列表）
    distances: List[List[float]]  # 距离（嵌套列表）
    ids: List[List[str]]  # 文档ID（嵌套列表）


# 空检索结果常量（不可变，避免被意外修改）
EMPTY_RAW_RETRIEVAL_RESULT: RawRetrievalResult = MappingProxyType({  # type: ignore[assignment]
    SchemaKeys.RR_DOCUMENTS: [[]],
    SchemaKeys.RR_METADATAS: [[]],
    SchemaKeys.RR_DISTANCES: [[]],
    SchemaKeys.RR_IDS: [[]]
})


class ChildDocumentMetadata(TypedDict, total=False):
    """
    子文档元数据结构
    
    使用场景：在检索阶段，存储子文档的所有元数据信息。
    """
    child_chunk_id: str  # 子文档ID（格式：parent_chapter_1_child_0）
    parent_id: str  # 父文档ID
    child_chunk_index: int  # 在父文档中的子块索引
    total_child_chunks: int  # 父文档的总子块数
    source: str  # 来源文件路径
    chapter_index: Optional[int]  # 章节索引
    chapter_title: Optional[str]  # 章节标题
    scene_index: Optional[int]  # 场景索引
    scene_title: Optional[str]  # 场景标题
    keywords: Union[List[str], str]  # 关键词列表（可能是JSON字符串）
    segmented_words: Union[List[str], str]  # 分词结果（可能是JSON字符串）
    match_type: Optional[str]  # 匹配类型（"vector", "bm25", "graph", "keyword", "hybrid", "multi_source"）
    matched_keywords: Optional[List[str]]  # 匹配的关键词（用于关键词匹配场景）


class ChildDocument(TypedDict, total=False):
    """
    子文档数据结构
    
    使用场景：在检索阶段，从原始检索结果（RawRetrievalResult）转换而来。
    用于检索、重排序和融合。
    
    字段说明：
    - content: 文档内容文本
    - metadata: 文档元数据（包含所有元信息）
    - distance: 距离分数（越小越相似，0表示完全匹配）
    - child_id: 子文档ID（从metadata['child_chunk_id']提取，格式：parent_chapter_1_child_0）
    - parent_id: 父文档ID（从metadata['parent_id']提取）
    - match_type: 匹配类型（"vector", "bm25", "graph", "keyword", "hybrid", "multi_source"）
    - retrieval_sources: 召回源列表（["vector", "bm25", "graph"]）
    - graph_hit: 是否被图谱召回命中
    - bm25_hit: 是否被BM25召回命中
    - rerank_score: 重排序分数（可选，由Reranker生成）
    - rrf_score: RRF融合分数（可选，用于三路召回融合）
    - final_score: 最终分数（可选，用于排序）
    """
    content: str  # 文档内容
    metadata: ChildDocumentMetadata  # 文档元数据
    distance: Optional[float]  # 距离分数（None表示无距离信息）
    child_id: str  # 子文档ID
    parent_id: Optional[str]  # 父文档ID
    match_type: Optional[str]  # 匹配类型
    retrieval_sources: Optional[List[str]]  # 召回源列表
    graph_hit: Optional[bool]  # 是否被图谱召回命中
    graph_score: Optional[float]  # 图谱统一相关性总分（0~1），用于排序/融合
    bm25_hit: Optional[bool]  # 是否被BM25召回命中
    rerank_score: Optional[float]  # 重排序分数
    rrf_score: Optional[float]  # RRF融合分数
    final_score: Optional[float]  # 最终分数


@dataclass
class FusionConfig:
    """
    融合策略配置
    
    使用场景：在检索阶段，用于配置多路召回的融合策略和权重。
    
    字段说明：
    - strategy: 融合策略（"rrf" 或 "weighted"）
    - vector_weight: 向量召回权重（0-1之间）
    - bm25_weight: BM25召回权重（0-1之间）
    - graph_weight: 图谱召回权重（0-1之间）
    - rrf_k: RRF常数（用于RRF策略）
    """
    strategy: str = "rrf"  # 融合策略："rrf" 或 "weighted"
    vector_weight: float = 0.4  # 向量召回权重（0-1之间）
    bm25_weight: float = 0.3  # BM25召回权重（0-1之间）
    graph_weight: float = 0.3  # 图谱召回权重（0-1之间）
    rrf_k: int = 60  # RRF常数（用于RRF策略）

    def __post_init__(self) -> None:
        """Validate fusion config weights."""
        total = self.vector_weight + self.bm25_weight + self.graph_weight
        if abs(total - 1.0) > 1e-6:
            raise RAGConfigError(
                f"Fusion weights must sum to 1.0, got {total:.4f} "
                f"(vector={self.vector_weight}, bm25={self.bm25_weight}, graph={self.graph_weight})"
            )
        if self.strategy not in ("rrf", "weighted"):
            raise RAGConfigError(
                f"Unsupported fusion strategy: '{self.strategy}', must be 'rrf' or 'weighted'"
            )


# ==================== 4. 结果返回 ====================

class ParentDocumentMetadata(TypedDict, total=False):
    """
    父文档元数据结构
    
    使用场景：在结果返回阶段，存储父文档的所有元数据信息。
    """
    parent_id: str  # 父文档ID
    title: Optional[str]  # 文档标题
    source: Optional[str]  # 来源文件路径
    chapter_index: Optional[int]  # 章节索引
    chapter_title: Optional[str]  # 章节标题
    scene_index: Optional[int]  # 场景索引
    scene_title: Optional[str]  # 场景标题


class ParentDocument(TypedDict, total=False):
    """
    父文档数据结构
    
    使用场景：在结果返回阶段，从子文档（ChildDocument）聚合而来。
    用于返回完整的文档上下文给用户。
    
    字段说明：
    - parent_id: 父文档ID
    - parent_content: 父文档完整内容
    - parent_metadata: 父文档元数据
    - matched_children: 匹配的子文档列表
    - matched_children_count: 匹配的子文档数量
    - best_distance: 最佳距离（所有子文档中的最小距离）
    - relevance_score: 相关性分数（综合所有子文档）
    - rerank_score: 重排序分数（可选）
    - retrieval_sources: 召回源列表
    - graph_hit: 是否被图谱召回命中
    - bm25_hit: 是否被BM25召回命中
    - rrf_score: RRF融合分数（可选）
    """
    parent_id: str  # 父文档ID
    parent_content: str  # 父文档完整内容
    parent_metadata: ParentDocumentMetadata  # 父文档元数据
    matched_children: List[ChildDocument]  # 匹配的子文档列表
    matched_children_count: int  # 匹配的子文档数量
    best_distance: Optional[float]  # 最佳距离（所有子文档中的最小距离）
    relevance_score: float  # 相关性分数（综合所有子文档）
    rerank_score: Optional[float]  # 重排序分数（可选）
    retrieval_sources: Optional[List[str]]  # 召回源列表
    graph_hit: Optional[bool]  # 是否被图谱召回命中
    bm25_hit: Optional[bool]  # 是否被BM25召回命中
    rrf_score: Optional[float]  # RRF融合分数（可选）


# ==================== 5. 工具与校验 ====================

def create_empty_query_analysis() -> QueryAnalysis:
    """
    创建空的查询分析结果
    
    Returns:
        包含所有必需字段的默认查询分析结果
    """
    _K = SchemaKeys
    return {
        _K.QA_INTENT: "",
        _K.QA_ENTITIES: [],
        _K.QA_STRATEGIES: [],
        _K.QA_RELATION_TYPE: None,
        _K.QA_KEYWORDS: [],
        _K.QA_SEGMENTED_WORDS: [],
        _K.QA_EXPANDED_ENTITIES: [],
        _K.QA_STANDARD_ENTITY_NAMES: [],
        _K.QA_NEED_REASONING: False,
        _K.QA_REASONING_TYPE: None,
    }


def create_empty_child_document() -> ChildDocument:
    """
    创建空的子文档结构
    
    Returns:
        包含所有必需字段的默认子文档
    """
    _K = SchemaKeys
    return {
        _K.CD_CONTENT: "",
        _K.CD_METADATA: {},
        _K.CD_DISTANCE: None,
        _K.CD_CHILD_ID: "",
        _K.CD_PARENT_ID: None,
        _K.CD_MATCH_TYPE: None,
        _K.CD_RETRIEVAL_SOURCES: None,
        _K.CD_GRAPH_HIT: None,
        _K.CD_GRAPH_SCORE: None,
        _K.CD_BM25_HIT: None,
        _K.CD_RERANK_SCORE: None,
        _K.CD_RRF_SCORE: None,
        _K.CD_FINAL_SCORE: None,
    }


def create_empty_parent_document() -> ParentDocument:
    """
    创建空的父文档结构
    
    Returns:
        包含所有必需字段的默认父文档
    """
    _K = SchemaKeys
    return {
        _K.PD_PARENT_ID: "",
        _K.PD_PARENT_CONTENT: "",
        _K.PD_PARENT_METADATA: {},
        _K.PD_MATCHED_CHILDREN: [],
        _K.PD_MATCHED_CHILDREN_COUNT: 0,
        _K.PD_BEST_DISTANCE: None,
        _K.PD_RELEVANCE_SCORE: 0.0,
        _K.PD_RERANK_SCORE: None,
        _K.PD_RETRIEVAL_SOURCES: None,
        _K.PD_GRAPH_HIT: None,
        _K.PD_BM25_HIT: None,
        _K.PD_RRF_SCORE: None,
    }


def validate_query_analysis_structure(query_analysis: Dict[str, Any]) -> QueryAnalysis:
    """
    验证并标准化query_analysis结构
    
    Args:
        query_analysis: 查询分析结果字典
        
    Returns:
        标准化后的QueryAnalysis
        
    Raises:
        RAGConfigError: 如果结构不合法
    """
    _K = SchemaKeys
    if not isinstance(query_analysis, dict):
        raise RAGConfigError("query_analysis must be a dict")

    raw_strategies = query_analysis.get(_K.QA_STRATEGIES, [])
    strategies = list(raw_strategies) if isinstance(raw_strategies, list) else []
    strategies = [str(s).strip() for s in strategies if s and str(s).strip()]

    result: QueryAnalysis = {
        _K.QA_INTENT: str(query_analysis.get(_K.QA_INTENT, "")),
        _K.QA_ENTITIES: query_analysis.get(_K.QA_ENTITIES, []),
        _K.QA_STRATEGIES: strategies,
        _K.QA_RELATION_TYPE: query_analysis.get(_K.QA_RELATION_TYPE),
        _K.QA_KEYWORDS: query_analysis.get(_K.QA_KEYWORDS, []),
        _K.QA_SEGMENTED_WORDS: query_analysis.get(_K.QA_SEGMENTED_WORDS, []),
        _K.QA_NEED_REASONING: bool(query_analysis.get(_K.QA_NEED_REASONING, False)),
        _K.QA_REASONING_TYPE: query_analysis.get(_K.QA_REASONING_TYPE),
    }

    # Type validation: normalize entities to string list (dict -> extract text)
    raw_entities = result[_K.QA_ENTITIES]
    if not isinstance(raw_entities, list):
        result[_K.QA_ENTITIES] = []
    else:
        normalized = []
        for e in raw_entities:
            if isinstance(e, str) and e.strip():
                normalized.append(e.strip())
            elif isinstance(e, dict) and e.get(_K.ENT_TEXT):
                normalized.append(str(e.get(_K.ENT_TEXT, "")).strip())
        result[_K.QA_ENTITIES] = normalized
    if not isinstance(result[_K.QA_SEGMENTED_WORDS], list):
        result[_K.QA_SEGMENTED_WORDS] = []
    if not isinstance(result[_K.QA_KEYWORDS], list):
        result[_K.QA_KEYWORDS] = []

    return result


def validate_child_document_structure(child_doc: Dict[str, Any]) -> ChildDocument:
    """
    验证并标准化子文档结构
    
    Args:
        child_doc: 子文档字典
        
    Returns:
        标准化后的ChildDocument
        
    Raises:
        RAGConfigError: 如果必需字段缺失
    """
    _K = SchemaKeys
    if not isinstance(child_doc, dict):
        raise RAGConfigError("child_doc must be a dict")

    if _K.CD_CONTENT not in child_doc:
        raise RAGConfigError(f"child_doc must contain '{_K.CD_CONTENT}' field")
    if _K.CD_CHILD_ID not in child_doc:
        raise RAGConfigError(f"child_doc must contain '{_K.CD_CHILD_ID}' field")

    result: ChildDocument = {
        _K.CD_CONTENT: str(child_doc[_K.CD_CONTENT]),
        _K.CD_METADATA: child_doc.get(_K.CD_METADATA, {}),
        _K.CD_DISTANCE: child_doc.get(_K.CD_DISTANCE),
        _K.CD_CHILD_ID: str(child_doc[_K.CD_CHILD_ID]),
        _K.CD_PARENT_ID: child_doc.get(_K.CD_PARENT_ID),
        _K.CD_MATCH_TYPE: child_doc.get(_K.CD_MATCH_TYPE),
        _K.CD_RETRIEVAL_SOURCES: child_doc.get(_K.CD_RETRIEVAL_SOURCES),
        _K.CD_GRAPH_HIT: child_doc.get(_K.CD_GRAPH_HIT),
        _K.CD_GRAPH_SCORE: child_doc.get(_K.CD_GRAPH_SCORE),
        _K.CD_BM25_HIT: child_doc.get(_K.CD_BM25_HIT),
        _K.CD_RERANK_SCORE: child_doc.get(_K.CD_RERANK_SCORE),
        _K.CD_RRF_SCORE: child_doc.get(_K.CD_RRF_SCORE),
        _K.CD_FINAL_SCORE: child_doc.get(_K.CD_FINAL_SCORE),
    }
    return result
