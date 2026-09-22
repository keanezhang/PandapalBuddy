#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于LLM的实体和关系联合抽取器

使用LLM一次性完成实体抽取和关系抽取，适合处理复杂文本。

【位置】agent/rag/build/data/entity_relationship_extractor_llm.py

建议通过 LexicalEnricher 使用，而不是直接调用。
LexicalEnricher 会处理文档分割、元数据提取等前置工作。
"""

import json
import importlib.resources
import re
from typing import List, Optional, Dict, Any, Set, Tuple
from pathlib import Path
from ...rag_config import RAGLLMProvider
from ...schema import Entity, Relationship, RelationType, ChunkLexicalInfo, ExtractionResult, SchemaKeys
from ...instructions.instruction_loader import load_instruction
from ... import instructions as _instructions_pkg  # 模块对象引用，零包名耦合

import logging
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 有效的实体标签集合（与 store.graph.entity_type 解耦，避免 build→store 跨层依赖）
# ---------------------------------------------------------------------------
VALID_ENTITY_LABELS: Dict[str, str] = {
    "PER": "Person",
    "LOC": "Location",
    "ORG": "Organization",
    "EVENT": "Event",
}

# ---------------------------------------------------------------------------
# LLM 调用默认参数（与 store.graph.graph_constants 解耦）
# ---------------------------------------------------------------------------
DEFAULT_TEMPERATURE: float = 0.3
DEFAULT_MAX_TURNS: int = 1

# ---------------------------------------------------------------------------
# 联合抽取器配置（Agent 名、token 上限、置信度、预览长度等）
# ---------------------------------------------------------------------------
DEFAULT_MAX_TOKENS_JOINT: int = 30000  # 输出 JSON 可能很长，指令 .md 中 {DEFAULT_MAX_TOKENS_JOINT} 也用它
AGENT_NAME_ENTITY_RELATIONSHIP_EXTRACTOR: str = "entity_relationship_extractor_by_llm"
DEFAULT_CONFIDENCE_JOINT: float = 0.85
DEFAULT_CONTENT_PREVIEW_LENGTH: int = 500
DEFAULT_DESCRIPTION_PREVIEW_LENGTH: int = 50

# 文本匹配前缀长度（用于 source_text / source_texts 匹配到子文档时的截断长度）
MATCH_PREFIX_LENGTH: int = 80
# 位置预览字符数（在 prompt 中展示 chunk 开头预览）
POSITION_PREVIEW_CHARS: int = 100
# 内容预览长度（调试用：记录第一个 chunk 的前 N 字）
CHUNK_CONTENT_PREVIEW_LENGTH: int = 25

# ---------------------------------------------------------------------------
# 指令文件（加载 entity_relationship_extractor_instruction.md 用）
# 通过 importlib.resources + 模块对象定位 instructions 包，不硬编码包名
# ---------------------------------------------------------------------------
_EXTRACTOR_INSTRUCTION_ID = "entity_relationship_extractor_instruction"
_EXTRACTOR_INSTRUCTION_FILENAME = "entity_relationship_extractor_instruction.md"


def _get_extractor_instructions_dir() -> Path:
    """通过 importlib.resources 定位 instructions 包的文件系统路径。"""
    ref = importlib.resources.files(_instructions_pkg)
    return Path(str(ref))

# ---------------------------------------------------------------------------
# 指令占位符内容（填入 .md 的 {ENTITY_LABELS_SECTION} / {RELATION_TYPES_SECTION}）
# 以下为默认通用模板，用户可通过构造函数传入自定义内容覆盖
# ---------------------------------------------------------------------------
_DEFAULT_ENTITY_LABELS_SECTION = """
* PER（人物/人名：各类角色名，包括道号、法号、尊号、绰号等）
* LOC（地名：包括但不限于城市、山脉、河流、建筑、区域等地理实体）
* ORG（组织名：包括但不限于公司、机构、团体、家族、联盟等组织）
* EVENT（事件：情节中的可命名事件，如核心事件、转折点等）
""".strip()

_DEFAULT_RELATION_TYPES_SECTION = """【人物之间关系类型】
MASTER: 师徒
FRIEND: 朋友
ENEMY: 敌人
FAMILY: 家族
SPOUSE: 夫妻/伴侣
SCHOOLFELLOW: 同门/同学
SUBORDINATE: 上下级
ALLY: 盟友
MENTOR: 前辈指导
RIVAL: 对手
LOVER: 恋人
NEIGHBOR: 邻居
CREDITOR: 债主
DEBTOR: 债务人

【人物与地点关系类型】
RESIDES_IN: 居住关系
BORN_IN: 出生地关系
ACTIVITY_AT: 活动/学习地点
PASSES_THROUGH: 途经关系
RULES: 统治/管理
GUARDIAN_OF: 守护

【人物与组织关系类型】
BELONGS_TO: 属于
FOUNDED: 创立
LEADER_OF: 领导
LEFT: 离开
BETRAYED: 背叛

【事件相关关系类型】
PARTICIPATES_IN: 参与事件
TRIGGERS: 触发
CAUSES: 导致
OCCURS_AT: 发生地点
NARRATIVE_FOCUS: 叙事焦点
SELECTED_BY: 被选中"""


def _build_extractor_instruction_variables(
    entity_labels_section: Optional[str] = None,
    relation_types_section: Optional[str] = None,
) -> Dict[str, Any]:
    """返回指令 .md 的占位变量（三个键，直接引用常量）。"""
    return {
        "DEFAULT_MAX_TOKENS_JOINT": DEFAULT_MAX_TOKENS_JOINT,
        "RELATION_TYPES_SECTION": relation_types_section or _DEFAULT_RELATION_TYPES_SECTION,
        "ENTITY_LABELS_SECTION": entity_labels_section or _DEFAULT_ENTITY_LABELS_SECTION,
    }


# 为了让 LLM 能为每个子文档产出 lexical（分词/关键词），需要在 prompt 中提供子文档内容。
# 但为控制 token 成本，这里对每个 chunk 的内容做上限截断（字符数）。
_MAX_CHILD_CHUNK_CONTENT_CHARS_FOR_LEXICAL = 2000


def _is_chinese_word(word: str) -> bool:
    """
    检查词元是否只包含中文字符（不含英文字母）。

    用于过滤 LLM 返回的 segmented_words / keywords 中的非中文词元。
    """
    if not word or not word.strip():
        return False
    has_chinese = any('\u4e00' <= char <= '\u9fff' for char in word)
    has_english = any(char.isalpha() and ord(char) < 128 for char in word)
    return has_chinese and not has_english


def _escape_control_chars_in_json(json_text: str) -> str:
    """在JSON字符串值中转义控制字符（状态机方法）。"""
    result = []
    i = 0
    in_string = False
    escape_next = False

    while i < len(json_text):
        char = json_text[i]

        if escape_next:
            code = ord(char)
            if 0 <= code < 32:
                result.append(f'\\u{code:04x}')
            else:
                result.append(char)
            escape_next = False
        elif char == '\\':
            result.append(char)
            escape_next = True
        elif char == '"':
            result.append(char)
            in_string = not in_string
        elif in_string:
            code = ord(char)
            if 0 <= code < 32:
                result.append(f'\\u{code:04x}')
            else:
                result.append(char)
        else:
            result.append(char)

        i += 1

    return ''.join(result)


class LLMEntityRelationshipExtractor:
    """
    基于LLM的实体和关系联合抽取器
    
    使用LLM一次性完成实体抽取和关系抽取，适合处理复杂文本
    与传统的"先NER抽取实体，再用LLM抽取关系"的方法对比
    """

    def __init__(
        self,
        llm_provider: RAGLLMProvider,
        verbose: bool = True,
        entity_labels_section: Optional[str] = None,
        relation_types_section: Optional[str] = None,
        instructions: Optional[str] = None,
    ):
        """
        初始化LLM实体和关系联合抽取器
        
        Args:
            llm_provider: 用于实体/关系抽取的 RAGLLMProvider 实现（必需）
            verbose: 是否输出详细信息
            entity_labels_section: 自定义实体标签描述文本，替换默认模板
            relation_types_section: 自定义关系类型描述文本，替换默认模板
            instructions: 外部传入的完整指令文本（可选）。传入则直接使用，
                忽略 entity_labels_section 和 relation_types_section；
                不传则从内置 .md 模板加载。
        """
        self.verbose = verbose
        if llm_provider is None:
            raise ValueError("llm_provider 不能为空，请提供有效的 RAGLLMProvider 实例")
        self.llm_provider = llm_provider
        self._entity_labels_section = entity_labels_section
        self._relation_types_section = relation_types_section

        # 构建Agent的指令：外部传入优先，内置 .md 作为默认值
        self._agent_instructions = instructions if instructions else self._build_agent_instructions()

        if self.verbose:
            source = "外部传入" if instructions else "内置 .md"
            logger.info("LLMEntityRelationshipExtractor 初始化完成，指令来源: %s", source)

    def _build_agent_instructions(self) -> str:
        """构建指令：加载 .md 并注入变量。"""
        variables = _build_extractor_instruction_variables(
            entity_labels_section=self._entity_labels_section,
            relation_types_section=self._relation_types_section,
        )
        instructions_dir = _get_extractor_instructions_dir()
        instruction_file = instructions_dir / _EXTRACTOR_INSTRUCTION_FILENAME
        return load_instruction(instruction_file, variables=variables)

    def get_instruction(self) -> str:
        """返回当前加载的指令全文（变量已替换），便于审计或保存最终组合 prompt。"""
        return self._agent_instructions

    def get_combined_prompt_sample(
        self,
        text: str = "",
        child_chunks: Optional[List[Dict[str, Any]]] = None,
        user_message_only: bool = False,
    ) -> str:
        """
        返回「指令 + 用户消息」组合后的 prompt 样本，便于调试或落盘审计。

        Args:
            text: 用户原文（可为空或截断样本）
            child_chunks: 子文档列表（可选），用于生成 chunk_info_section
            user_message_only: 若为 True，只返回用户消息部分，否则返回「指令 + 分隔 + 用户消息」

        Returns:
            组合后的完整 prompt 字符串
        """
        user_msg = self._build_prompt(text or "（此处为原文，实际运行时会替换为完整章节内容）", child_chunks)
        if user_message_only:
            return user_msg.strip()
        sep = "\n\n" + "=" * 60 + "\n【用户消息 / User Message】\n" + "=" * 60 + "\n\n"
        return (self._agent_instructions or "") + sep + user_msg.strip()

    def _match_text_to_chunks(
        self,
        source_texts: List[str],
        chunks_for_match: List[Tuple[str, str, str]],
        valid_chunk_ids: set,
        max_results: int = 5
    ) -> List[str]:
        """
        通用的文本到chunk映射方法（消除重复逻辑）
        
        Args:
            source_texts: 待匹配的源文本列表
            chunks_for_match: chunk三元组列表 [(chunk_id, content, normalized_content), ...]
            valid_chunk_ids: 有效的chunk_id集合
            max_results: 最多返回的chunk数量
            
        Returns:
            匹配到的chunk_id列表
        """
        chunk_ids = []
        
        for st in source_texts[:max_results]:
            if not st:
                continue
            st_str = str(st)
            st_norm = "".join(st_str.split())
            if not st_norm:
                continue

            # 先尝试完整匹配；若失败再用前缀匹配（降低严格度）
            needles = [st_norm, st_norm[:MATCH_PREFIX_LENGTH]] if len(st_norm) > MATCH_PREFIX_LENGTH else [st_norm]
            matched = False
            for needle in needles:
                if not needle:
                    continue
                for cid, _ctext, ctext_norm in chunks_for_match:
                    if needle in ctext_norm:
                        if cid in valid_chunk_ids and cid not in chunk_ids:
                            chunk_ids.append(cid)
                        matched = True
                        break
                if matched:
                    break

            if len(chunk_ids) >= max_results:
                break
        
        return chunk_ids

    async def extract_entities_and_relationships(
        self,
        text: str,
        doc_id: Optional[str] = None,
        chunk_id: Optional[str] = None,
        book_title: Optional[str] = None,
        chapter_info: Optional[Dict[str, Any]] = None,
        child_chunks: Optional[List[Dict[str, Any]]] = None
    ) -> ExtractionResult:
        """
        使用LLM从文本中同时提取实体和关系（异步方法）
        
        【重要说明】关于章节/场景信息的保存：
        1. 为了支持RAG三路找回，必须正确记录章节和场景信息
        2. 建议使用父文档（完整的章节/场景）进行抽取，而不是子文档片段
            - 原因：保持语义完整性，避免跨块语义丢失
            - 方法：使用 GraphBuilder.build_graph_batch() 传入父文档
        3. 章节/场景信息会保存在：
            - Entity: first_chapter_index, first_chapter_title, first_scene_index, first_scene_title
            - Relationship: chapter_index, chapter_title, scene_index, scene_title
        
        Args:
            text: 输入文本（建议是完整的章节/场景内容，而非子文档片段）
            doc_id: 文档ID
            chunk_id: 文档块ID（在父文档场景下，应传入parent_chunk_id；在子文档场景下，传入子文档的child_chunk_id）
            book_title: 书名（用于双路召回）
            chapter_info: 章节信息字典（可选），包含 chapter_index, chapter_title, scene_index, scene_title
            child_chunks: 子文档位置信息列表（可选），每个元素应包含：
                - chunk_id: 子文档ID（child_chunk_id格式，必需，如parent_chapter_1_child_0）
                - start_pos: 在父文档中的起始字符位置（推荐，用于位置映射）
                - end_pos: 在父文档中的结束字符位置（推荐，用于位置映射）
                - content: 子文档内容（可选，仅当无法提供位置信息时作为fallback）
                如果提供，LLM会在提取关系时同时判断关系最可能出现在哪个chunk
                推荐使用位置信息（start_pos/end_pos），避免重复传入完整内容，减少token消耗
            
        Returns:
            ExtractionResult：实体、关系，以及（可选）子文档词法分析结果
        """
        try:
            # 构建提示词（如果提供了child_chunks，会在提示词中包含chunk信息）
            prompt = self._build_prompt(text, child_chunks)

            # 使用 llm_provider 调用 LLM
            import time as _time
            if self.verbose:
                prompt_len = len(prompt)
                instr_len = len(self._agent_instructions) if self._agent_instructions else 0
                logger.info(
                    "  ⏳ 正在调用 LLM（prompt=%s字符, instructions=%s字符, max_tokens=%s）请等待...",
                    prompt_len, instr_len, DEFAULT_MAX_TOKENS_JOINT,
                )
                logger.info(
                    "  ℹ 若长时间无响应请检查网络与 API 可用性；多数实现默认约 5 分钟超时。"
                )
            _t_llm_start = _time.time()

            content = await self.llm_provider.run(
                prompt,
                self._agent_instructions,
                agent_name=AGENT_NAME_ENTITY_RELATIONSHIP_EXTRACTOR,
                temperature=DEFAULT_TEMPERATURE,
                max_tokens=DEFAULT_MAX_TOKENS_JOINT,
                timeout=300,  # 5 分钟，部分实现（如 AgentManagerRAGLLMProvider）会使用
            )
            content = (content or "").strip()

            _t_llm_elapsed = _time.time() - _t_llm_start
            if self.verbose:
                logger.info("  ✅ LLM 返回（耗时 %.1f 秒，响应 %s 字符）", _t_llm_elapsed, len(content))

            # 调试信息：仅在 verbose 模式下输出
            if self.verbose:
                logger.debug(" LLM返回的完整内容（长度: %s 字符）", len(content))
                logger.debug("内容预览: %s", content)

            # 解析响应（如果提供了child_chunks，会解析chunk_index并映射到chunk_id）
            extraction = self._parse_response(
                content, doc_id, chunk_id, book_title, chapter_info, child_chunks
            )

            if self.verbose:
                logger.info(
                    "LLM抽取到 %s 个实体，%s 个关系，%s 个chunk词法结果",
                    len(extraction.entities), len(extraction.relationships), len(extraction.lexical)
                )

            return extraction

        except Exception as e:
            if self.verbose:
                logger.warning("LLM实体和关系抽取失败: %s", e, exc_info=True)
            return ExtractionResult(error=str(e))

    def _validate_chunk_positions(self, child_chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        验证子文档位置信息（统一的位置信息检查方法）
        
        Args:
            child_chunks: 子文档列表
            
        Returns:
            包含验证结果的字典：
            - has_position_info: 是否有任何chunk包含位置信息
            - position_count: 包含位置信息的chunk数量
            - valid_chunks: 包含有效位置信息的chunk列表（start_pos和end_pos都不为None）
        """
        if not child_chunks:
            return {
                'has_position_info': False,
                'position_count': 0,
                'valid_chunks': []
            }
        
        position_count = 0
        valid_chunks = []
        
        for chunk in child_chunks:
            start_pos = chunk.get('start_pos')
            end_pos = chunk.get('end_pos')
            
            # 检查是否有位置信息（至少有一个不为None）
            if start_pos is not None or end_pos is not None:
                position_count += 1
                
                # 检查是否有完整的位置信息（两个都不为None）
                if start_pos is not None and end_pos is not None:
                    valid_chunks.append(chunk)
        
        return {
            'has_position_info': position_count > 0,
            'position_count': position_count,
            'valid_chunks': valid_chunks
        }

    def _build_prompt(self, text: str, child_chunks: Optional[List[Dict[str, Any]]] = None) -> str:
        """
        构建完整的任务提示词（一次性包含所有规则、流程和输入）
        
        Args:
            text: 输入文本（父文档内容）
            child_chunks: 子文档位置信息列表（可选），每个chunk应该包含：
                - chunk_id: 子文档ID（child_chunk_id格式，必需，如parent_chapter_1_child_0）
                - start_pos: 在父文档中的起始字符位置（必需）
                - end_pos: 在父文档中的结束字符位置（必需）
        """
        # 构建子文档位置信息部分（如果提供）
        chunk_info_section = self._build_chunk_info_section(text, child_chunks)
        
        # 一次性构建完整的prompt，包含所有规则和流程
        prompt = f"""
用户给定的原文文档和子文档位置信息：{text}{chunk_info_section}
"""
        
        return prompt
    
    def _build_chunk_info_section(
        self, 
        text: str, 
        child_chunks: Optional[List[Dict[str, Any]]]
    ) -> str:
        """
        构建子文档位置信息部分
        
        Args:
            text: 父文档内容
            child_chunks: 子文档位置信息列表
            
        Returns:
            子文档位置信息的字符串描述，如果没有有效信息则返回空字符串
        """
        if not child_chunks or len(child_chunks) <= 1:
            return ""
        
        # 验证位置信息
        position_validation = self._validate_chunk_positions(child_chunks)
        valid_chunks = position_validation['valid_chunks']
        
        if not valid_chunks:
            if self.verbose:
                logger.warning(
" 子文档缺少位置信息（start_pos/end_pos），"
                    "无法在prompt中添加chunk信息。建议使用 build_chunk_position_map_batch 计算位置信息。"
                )
            return ""
        
        # 构建chunk信息列表（遍历 valid_chunks 保证片段编号连续且与有效 chunk 一致）
        chunks_info_parts = []
        for idx, chunk in enumerate(valid_chunks, 1):
            chunk_id_val = chunk.get(SchemaKeys.CDM_CHILD_CHUNK_ID, f'child_{idx}')
            start_pos = chunk.get('start_pos')
            end_pos = chunk.get('end_pos')
            # valid_chunks 已保证 start_pos/end_pos 非空
            preview = text[start_pos:min(start_pos + POSITION_PREVIEW_CHARS, end_pos)]
            chunk_content = chunk.get("content", "")
            if not isinstance(chunk_content, str):
                chunk_content = str(chunk_content) if chunk_content is not None else ""
            chunk_content = chunk_content.strip()
            if chunk_content and len(chunk_content) > _MAX_CHILD_CHUNK_CONTENT_CHARS_FOR_LEXICAL:
                chunk_content = chunk_content[:_MAX_CHILD_CHUNK_CONTENT_CHARS_FOR_LEXICAL] + "..."
            
            # 使用列表收集字符串片段，最后统一join（避免循环中+=拼接）
            chunk_info = [
                f"\n片段{idx} (child_chunk_id: {chunk_id_val}):\n",
                f"  位置: 字符 {start_pos} - {end_pos} (共 {end_pos - start_pos} 字符)\n",
                f"  开头: {preview}...\n"
            ]
            if chunk_content:
                chunk_info.append(f"  内容: {chunk_content}\n")
            
            chunks_info_parts.append("".join(chunk_info))
        
        chunks_info = "".join(chunks_info_parts)

        if not chunks_info:
            return ""
        
        # 构建chunk信息部分（只包含位置信息，判断规则已在主prompt的步骤4中说明）
        chunk_info_section = f"""

【子文档位置信息】
以下是将上述完整文本分割后的子文档位置信息（用于步骤4判断实体和关系的chunk归属）：
{chunks_info}
"""
        
        return chunk_info_section

    def _parse_relation_type(self, relation_type_str: str) -> RelationType:
        """
        解析关系类型字符串，支持类型映射和容错
        
        Args:
            relation_type_str: LLM返回的关系类型字符串
            
        Returns:
            RelationType枚举值
        """
        if not relation_type_str:
            return RelationType.UNKNOWN

        # 去除空格并转换为大写
        relation_type_str = relation_type_str.strip().upper()

        # 尝试直接匹配枚举值
        try:
            return RelationType[relation_type_str]
        except KeyError:
            # 如果无法匹配，记录警告并返回UNKNOWN
            if self.verbose:
                logger.warning(
                    "未知的关系类型: '%s'，已映射为UNKNOWN。可用的关系类型: %s",
                    relation_type_str, ', '.join([rt.value for rt in RelationType if rt != RelationType.UNKNOWN])
                )
            return RelationType.UNKNOWN

    def _extract_json_from_markdown(self, content: str) -> str:
        """从可能包含markdown代码块的内容中提取JSON字符串。"""
        json_str = content.strip()

        if "```json" in content:
            start_marker = "```json"
            start_idx = content.find(start_marker)
            if start_idx >= 0:
                start_idx += len(start_marker)
                end_idx = content.find("```", start_idx)
                if end_idx > start_idx:
                    json_str = content[start_idx:end_idx].strip()
                else:
                    json_str = content[start_idx:].strip().lstrip('\n\r')
        elif "```" in content:
            start_idx = content.find("```")
            if start_idx >= 0:
                start_idx += len("```")
                end_idx = content.find("```", start_idx)
                if end_idx > start_idx:
                    json_str = content[start_idx:end_idx].strip()
                    if json_str.startswith("json"):
                        json_str = json_str[4:].strip()
                else:
                    json_str = content[start_idx:].strip().lstrip('\n\r')
                    if json_str.startswith("json"):
                        json_str = json_str[4:].strip()

        if json_str.startswith("```") or json_str.startswith("```json"):
            json_start = json_str.find("{")
            if json_start < 0:
                json_start = json_str.find("[")
            if json_start >= 0:
                json_str = json_str[json_start:].strip()
            else:
                json_str = content.strip()

        return json_str

    @staticmethod
    def _fix_trailing_commas(json_str: str) -> str:
        """移除JSON中的尾随逗号。"""
        return re.sub(r',(\s*[}\]])', r'\1', json_str)

    @staticmethod
    def _fix_truncated_json(json_str: str) -> str:
        """补全被截断的JSON（缺少结束括号）。"""
        open_braces = json_str.count('{')
        close_braces = json_str.count('}')
        open_brackets = json_str.count('[')
        close_brackets = json_str.count(']')

        if open_braces > close_braces or open_brackets > close_brackets:
            missing_braces = open_braces - close_braces
            missing_brackets = open_brackets - close_brackets
            json_str = json_str + '}' * missing_braces + ']' * missing_brackets

        return json_str

    def _extract_and_parse_json(self, content: str) -> Optional[Dict[str, Any]]:
        """
        提取并解析JSON，支持多种格式和自动修复

        Args:
            content: LLM返回的原始内容（可能包含markdown代码块）

        Returns:
            解析后的字典，如果失败返回None
        """
        json_str = self._extract_json_from_markdown(content)

        if not json_str or not json_str.strip():
            if self.verbose:
                logger.warning(" 提取的JSON字符串为空，原始内容长度: %s 字符", len(content))
                logger.debug("   原始内容预览: %s", content[:200])
            return None

        # 尝试直接解析
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            fixed_json = json_str

            # 修复0: 转义控制字符
            try:
                fixed_json = _escape_control_chars_in_json(json_str)
                if fixed_json != json_str:
                    if self.verbose:
                        logger.debug("尝试修复JSON（转义控制字符）...")
                    return json.loads(fixed_json)
            except (json.JSONDecodeError, Exception) as fix_error:
                if self.verbose:
                    logger.debug("   控制字符转义修复失败: %s", fix_error)

            # 修复1: 移除尾随逗号
            try:
                fixed_json = self._fix_trailing_commas(fixed_json)
                if fixed_json != json_str:
                    if self.verbose:
                        logger.debug("尝试修复JSON（移除尾随逗号）...")
                    return json.loads(fixed_json)
            except (json.JSONDecodeError, Exception):
                pass

            # 修复2: 补全被截断的JSON
            try:
                fixed_json = self._fix_truncated_json(fixed_json)
                if self.verbose:
                    logger.debug("尝试修复JSON（补全被截断的JSON）...")
                return json.loads(fixed_json)
            except (json.JSONDecodeError, Exception):
                pass

            # 所有修复均失败
            if self.verbose:
                logger.info(" JSON解析失败，已尝试修复但仍失败: %s", e)
                logger.info("   原始内容长度: %s 字符", len(content))
                logger.info("   提取的JSON长度: %s 字符", len(json_str))
                logger.info("   JSON预览: %s...", json_str[:200])
                if hasattr(e, 'pos') and e.pos is not None:
                    error_pos = e.pos
                    ctx_start = max(0, error_pos - 50)
                    ctx_end = min(len(json_str), error_pos + 50)
                    logger.info("   错误位置附近的内容 (pos %s): %s", error_pos, json_str[ctx_start:ctx_end])
                    for ci in range(ctx_start, min(ctx_end, len(json_str))):
                        ch = json_str[ci]
                        if 0 <= ord(ch) < 32:
                            logger.info("   发现控制字符在位置 %s: %s (\\u%04x)", ci, repr(ch), ord(ch))
            return None

    def _parse_response(
        self,
        content: str,
        doc_id: Optional[str],
        chunk_id: Optional[str],
        book_title: Optional[str] = None,
        chapter_info: Optional[Dict[str, Any]] = None,
        child_chunks: Optional[List[Dict[str, Any]]] = None
    ) -> ExtractionResult:
        """
        解析LLM响应，提取实体和关系
        
        Args:
            content: LLM返回的JSON字符串
            doc_id: 文档ID
            chunk_id: 文档块ID（在父文档场景下是parent_chunk_id，在子文档场景下是子文档的child_chunk_id）
            book_title: 书名
            chapter_info: 章节信息字典，包含 chapter_index, chapter_title, scene_index, scene_title
            child_chunks: 子文档位置信息列表（用于chunk映射），每个元素包含chunk_id（child_chunk_id格式的子文档ID）
            
        Returns:
            (实体列表, 关系列表) 的元组
        """
        chapter_index = chapter_info.get('chapter_index') if chapter_info else None
        chapter_title = chapter_info.get('chapter_title') if chapter_info else None
        scene_index = chapter_info.get('scene_index') if chapter_info else None
        scene_title = chapter_info.get('scene_title') if chapter_info else None
        entities: List[Entity] = []
        relationships: List[Relationship] = []
        lexical: Dict[str, ChunkLexicalInfo] = {}

        try:
            # 构建子文档匹配表（供实体与关系解析共用）
            chunk_preview_map: Dict[str, str] = {}
            chunks_for_match: List[Tuple[str, str, str]] = []
            valid_chunk_ids: Set[str] = set()
            if child_chunks:
                for c in child_chunks:
                    try:
                        cid = c.get(SchemaKeys.CDM_CHILD_CHUNK_ID)
                        ctext = c.get("content", "")
                        if not cid or not isinstance(ctext, str):
                            continue
                        cid = str(cid)
                        preview = ctext.strip().replace("\n", " ").replace("\r", " ")[:CHUNK_CONTENT_PREVIEW_LENGTH]
                        chunk_preview_map[cid] = preview
                        ctext_norm = "".join(ctext.split())
                        chunks_for_match.append((cid, ctext, ctext_norm))
                        valid_chunk_ids.add(cid)
                    except Exception:
                        continue

            data = self._extract_and_parse_json(content)
            if data is None:
                raise json.JSONDecodeError("无法解析JSON", content, 0)

            if not isinstance(data, dict):
                if self.verbose:
                    logger.warning(" 响应格式不正确，期望字典，得到: %s", type(data))
                return ExtractionResult()

            # 分别解析实体、关系、词法
            chapter_meta = {
                "chapter_index": chapter_index,
                "chapter_title": chapter_title,
                "scene_index": scene_index,
                "scene_title": scene_title,
            }
            entities, entity_map = self._parse_entities_block(
                data, doc_id, book_title, chapter_meta,
                chunks_for_match, valid_chunk_ids, chunk_preview_map,
            )
            relationships = self._parse_relationships_block(
                data, doc_id, chunk_id, book_title, chapter_meta,
                entities, entity_map, chunks_for_match, valid_chunk_ids, chunk_preview_map,
            )
            lexical = self._parse_lexical_block(data)

        except json.JSONDecodeError as e:
            if self.verbose:
                logger.warning(" JSON解析失败: %s", e)
                if hasattr(e, 'lineno') and e.lineno and e.lineno > 0:
                    logger.debug("错误位置: line %s, column %s", e.lineno, e.colno)
                    lines = content.split('\n')
                    start_line = max(0, e.lineno - 3)
                    end_line = min(len(lines), e.lineno + 3)
                    logger.debug("错误附近的代码行 (%s-%s):", start_line + 1, end_line)
                    for i in range(start_line, end_line):
                        marker = ">>> " if i == e.lineno - 1 else "    "
                        logger.debug("%s%s: %s", marker, i + 1, lines[i])
        except Exception as e:
            if self.verbose:
                logger.warning(" 解析响应失败: %s", e, exc_info=True)
        
        return ExtractionResult(entities=entities, relationships=relationships, lexical=lexical)

    def _parse_entities_block(
        self,
        data: Dict[str, Any],
        doc_id: Optional[str],
        book_title: Optional[str],
        chapter_meta: Dict[str, Any],
        chunks_for_match: List[Tuple[str, str, str]],
        valid_chunk_ids: Set[str],
        chunk_preview_map: Dict[str, str],
    ) -> Tuple[List[Entity], Dict[str, str]]:
        """解析 LLM 返回的实体列表，返回 (entities, entity_map)。"""
        entities: List[Entity] = []
        entity_map: Dict[str, str] = {}

        entities_data = data.get(SchemaKeys.EXTRACTION_ENTITIES, [])
        if not isinstance(entities_data, list):
            entities_data = []

        for item in entities_data:
            try:
                name = item.get('name', '').strip()
                if not name:
                    continue

                label = item.get('label', 'PER').strip().upper()
                if label not in VALID_ENTITY_LABELS:
                    if self.verbose:
                        logger.warning(" 实体 '%s' 的label '%s' 无效，使用默认值 'PER'", name, label)
                    label = 'PER'

                aliases = item.get('aliases', [])
                if not isinstance(aliases, list):
                    aliases = []

                description = item.get('description', '')
                event_type_raw = item.get(SchemaKeys.ENT_EVENT_TYPE)
                event_type = str(event_type_raw).strip() if event_type_raw else None
                if event_type == '':
                    event_type = None
                frequency = int(item.get('frequency', 1))
                source_texts = item.get('source_texts', [])
                if not isinstance(source_texts, list):
                    source_texts = []

                # 实体 -> 子文档 chunk 映射（四级 fallback）
                chunk_ids: List[str] = []
                if chunks_for_match:
                    child_chunk_ids_from_llm = item.get(SchemaKeys.ENT_CHILD_CHUNK_IDS, [])
                    if isinstance(child_chunk_ids_from_llm, list) and child_chunk_ids_from_llm:
                        for cid in child_chunk_ids_from_llm:
                            if not cid:
                                continue
                            cid_str = str(cid)
                            if cid_str in valid_chunk_ids and cid_str not in chunk_ids:
                                chunk_ids.append(cid_str)

                    if not chunk_ids and source_texts and chunks_for_match:
                        chunk_ids = self._match_text_to_chunks(
                            source_texts=source_texts,
                            chunks_for_match=chunks_for_match,
                            valid_chunk_ids=valid_chunk_ids,
                            max_results=5
                        )

                    if not chunk_ids and chunks_for_match:
                        name_norm = "".join(name.split())
                        if name_norm:
                            scored: List[Tuple[int, str]] = []
                            for cid, _ctext, ctext_norm in chunks_for_match:
                                cnt = ctext_norm.count(name_norm)
                                if cnt > 0:
                                    scored.append((cnt, cid))
                            scored.sort(key=lambda x: x[0], reverse=True)
                            for _cnt, cid in scored[:5]:
                                if cid not in chunk_ids:
                                    chunk_ids.append(cid)

                    if not chunk_ids and chunks_for_match:
                        chunk_ids.append(chunks_for_match[0][0])

                    if self.verbose and chunk_ids:
                        logger.info(" 实体映射到chunks: %s (%s) -> chunk_ids=%s", name, label, chunk_ids)

                metadata = {'extraction_method': 'llm_joint'}
                if chunk_ids:
                    metadata['child_chunk_ids'] = chunk_ids
                    first_chunk_id = str(chunk_ids[0])
                    metadata['content_preview_chunk_id'] = first_chunk_id
                    metadata['content_preview_25'] = chunk_preview_map.get(first_chunk_id, "")

                entity = Entity(
                    name=name,
                    aliases=aliases,
                    label=label,
                    event_type=event_type,
                    description=description or None,
                    frequency=max(1, frequency),
                    first_appearance=doc_id,
                    first_book_title=book_title,
                    first_chapter_index=chapter_meta.get("chapter_index"),
                    first_chapter_title=chapter_meta.get("chapter_title"),
                    first_scene_index=chapter_meta.get("scene_index"),
                    first_scene_title=chapter_meta.get("scene_title"),
                    source_texts=source_texts,
                    metadata=metadata
                )

                entities.append(entity)
                entity_map[name.lower()] = name
                for alias in aliases:
                    entity_map[alias.lower()] = name

            except Exception as e:
                if self.verbose:
                    logger.debug(" 解析实体项失败: %s, 项: %s", e, item)
                continue

        return entities, entity_map

    def _parse_relationships_block(
        self,
        data: Dict[str, Any],
        doc_id: Optional[str],
        chunk_id: Optional[str],
        book_title: Optional[str],
        chapter_meta: Dict[str, Any],
        entities: List[Entity],
        entity_map: Dict[str, str],
        chunks_for_match: List[Tuple[str, str, str]],
        valid_chunk_ids: Set[str],
        chunk_preview_map: Dict[str, str],
    ) -> List[Relationship]:
        """解析 LLM 返回的关系列表。"""
        relationships: List[Relationship] = []

        relationships_data = data.get(SchemaKeys.EXTRACTION_RELATIONSHIPS, [])
        if not isinstance(relationships_data, list):
            relationships_data = []

        for item in relationships_data:
            try:
                source = item.get('source', '').strip()
                target = item.get('target', '').strip()
                relation_type_str = item.get('relation_type', 'UNKNOWN')
                description = item.get('description', '')
                confidence = float(item.get('confidence', DEFAULT_CONFIDENCE_JOINT))
                
                source_text_raw = item.get('source_text', '')
                if isinstance(source_text_raw, list):
                    source_text = '\n'.join(str(text_item) for text_item in source_text_raw if text_item)
                else:
                    source_text = str(source_text_raw) if source_text_raw else ''

                source_normalized = entity_map.get(source.lower(), source)
                target_normalized = entity_map.get(target.lower(), target)

                source_exists = any(e.name == source_normalized for e in entities)
                target_exists = any(e.name == target_normalized for e in entities)

                if not source_exists or not target_exists:
                    if self.verbose:
                        logger.debug(" 关系中的实体不存在，跳过: %s -> %s (source_exists=%s, target_exists=%s)", source, target, source_exists, target_exists)
                    continue

                if source_normalized.lower() == target_normalized.lower():
                    continue

                relation_type = self._parse_relation_type(relation_type_str)

                if relation_type == RelationType.UNKNOWN and self.verbose:
                    desc_preview = (description[:DEFAULT_DESCRIPTION_PREVIEW_LENGTH] + "...") if len(description) > DEFAULT_DESCRIPTION_PREVIEW_LENGTH else description
                    logger.warning(
                        "关系类型解析为UNKNOWN: %s -> %s, 原始类型: '%s', 描述: '%s'",
                        source_normalized, target_normalized, relation_type_str, desc_preview
                    )

                chunk_ids: List[str] = []
                mapped_chunk_id = chunk_id
                if chunks_for_match:
                    if source_text:
                        source_text_norm = "".join(source_text.split())
                        if source_text_norm:
                            needle = source_text_norm[:MATCH_PREFIX_LENGTH] if len(source_text_norm) > MATCH_PREFIX_LENGTH else source_text_norm
                            for cid, _ctext, ctext_norm in chunks_for_match:
                                if needle in ctext_norm or source_text_norm in ctext_norm:
                                    chunk_ids.append(cid)
                                    mapped_chunk_id = cid
                                    break
                    if not chunk_ids:
                        child_chunk_ids_from_llm = item.get(SchemaKeys.ENT_CHILD_CHUNK_IDS, [])
                        if isinstance(child_chunk_ids_from_llm, list) and child_chunk_ids_from_llm:
                            for chunk_id_item in child_chunk_ids_from_llm:
                                cid_str = str(chunk_id_item).strip() if chunk_id_item else ""
                                if cid_str and cid_str in valid_chunk_ids and cid_str not in chunk_ids:
                                    chunk_ids.append(cid_str)
                                    if len(chunk_ids) == 1:
                                        mapped_chunk_id = cid_str
                                    break
                    if not chunk_ids:
                        names_to_match = [source_normalized, target_normalized]
                        aliases_list: List[str] = []
                        for e in entities:
                            if e.name == source_normalized or e.name == target_normalized:
                                names_to_match.extend(e.aliases or [])
                                aliases_list.extend([a for a in (e.aliases or []) if a and len(str(a).strip()) >= 2])
                        names_to_match = [n for n in names_to_match if n and len(str(n).strip()) >= 2]
                        scored: List[Tuple[int, int, str]] = []
                        for cid, _ctext, ctext_norm in chunks_for_match:
                            cnt = sum(ctext_norm.count("".join(str(n).split())) for n in names_to_match)
                            has_alias = 1 if any(
                                "".join(str(a).split()) in ctext_norm for a in aliases_list
                            ) else 0
                            if cnt > 0 or has_alias:
                                scored.append((cnt, has_alias, cid))
                        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
                        if scored:
                            mapped_chunk_id = scored[0][2]
                            chunk_ids.append(scored[0][2])
                    if self.verbose and chunk_ids:
                        logger.debug(
                            "关系映射到chunks: %s -> %s (%s) -> chunk_id=%s",
                            source_normalized, target_normalized, relation_type.value, mapped_chunk_id
                        )

                metadata = {'extraction_method': 'llm_joint'}
                if chunk_ids:
                    metadata['child_chunk_ids'] = chunk_ids
                    if mapped_chunk_id:
                        mapped_id = str(mapped_chunk_id)
                        metadata['content_preview_chunk_id'] = mapped_id
                        metadata['content_preview_25'] = chunk_preview_map.get(mapped_id, "")

                relationship = Relationship(
                    source=source_normalized,
                    target=target_normalized,
                    relation_type=relation_type,
                    description=description or f"{relation_type.value}关系",
                    source_text=source_text,
                    confidence=confidence,
                    source_doc=doc_id,
                    source_chunk=mapped_chunk_id,
                    book_title=book_title,
                    chapter_index=chapter_meta.get("chapter_index"),
                    chapter_title=chapter_meta.get("chapter_title"),
                    scene_index=chapter_meta.get("scene_index"),
                    scene_title=chapter_meta.get("scene_title"),
                    metadata=metadata
                )

                relationships.append(relationship)

            except Exception as e:
                if self.verbose:
                    logger.debug(" 解析关系项失败: %s, 项: %s", e, item)
                continue

        return relationships

    def _parse_lexical_block(self, data: Dict[str, Any]) -> Dict[str, ChunkLexicalInfo]:
        """解析子文档词法分析（segmented_words, keywords）。"""
        lexical: Dict[str, ChunkLexicalInfo] = {}

        lexical_data = data.get(SchemaKeys.EXTRACTION_LEXICAL, [])
        if not isinstance(lexical_data, list) or not lexical_data:
            return lexical

        for item in lexical_data:
            try:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get(SchemaKeys.CDM_CHILD_CHUNK_ID, "")).strip()
                if not cid:
                    continue

                segmented_words = item.get(SchemaKeys.CDM_SEGMENTED_WORDS, [])
                keywords = item.get(SchemaKeys.CDM_KEYWORDS, [])

                if not isinstance(segmented_words, list):
                    segmented_words = []
                if not isinstance(keywords, list):
                    keywords = []

                segmented_words_clean = [str(w).strip() for w in segmented_words if str(w).strip() and _is_chinese_word(str(w).strip())]
                keywords_clean = [str(k).strip() for k in keywords if str(k).strip() and _is_chinese_word(str(k).strip())]
                
                if self.verbose and (not segmented_words_clean or not keywords_clean):
                    original_seg_count = len([w for w in segmented_words if str(w).strip()])
                    original_key_count = len([k for k in keywords if str(k).strip()])
                    if original_seg_count > 0 and not segmented_words_clean:
                        logger.warning(
                            "chunk %s 的 segmented_words 过滤后为空（原始: %s 个，可能包含非中文词元）",
                            cid, original_seg_count
                        )
                    if original_key_count > 0 and not keywords_clean:
                        logger.warning(
                            "chunk %s 的 keywords 过滤后为空（原始: %s 个，可能包含非中文词元）",
                            cid, original_key_count
                        )

                lexical[cid] = ChunkLexicalInfo(
                    child_chunk_id=cid,
                    segmented_words=segmented_words_clean,
                    keywords=keywords_clean,
                )
            except Exception as e:
                if self.verbose:
                    logger.debug(" 解析lexical失败: %s, 项: %s", e, item)
                continue

        return lexical
