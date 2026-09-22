#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查询中文分词器（QuerySegmenter）。

职责：仅对用户问句做「词元级中文分词」，输出 segmented_words，供 BM25 检索使用。

设计要点：
- 分词由**内部固定 LLM**完成（建库与检索同口径，避免 BM25 分词漂移）。
- 语义理解（意图/实体/关系/策略/关键词）由外部 LLM 通过 MCP 参数提供，本模块不涉及。
- 分词口径与建库抽取（entity_relationship_extractor_instruction.md）一致：
  词元级、过滤停用词/语气词/纯标点、禁止非中文。
"""

from __future__ import annotations

import importlib.resources
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...rag_protocol import RAGLLMProvider
from ...schema import SchemaKeys
from ...instructions.instruction_loader import load_instruction
from ... import instructions as _instructions_pkg  # 模块对象引用，零包名耦合

import logging

logger = logging.getLogger(__name__)

__all__ = ["QuerySegmenter"]

# 分词指令文件（位于 instructions 包内）
_SEGMENT_INSTRUCTION_ID = "query_segmenter_instruction"
_SEGMENT_INSTRUCTION_FILENAME = "query_segmenter_instruction.md"

# LLM 调用默认参数
_AGENT_NAME_QUERY_SEGMENTER: str = "query_segmenter"
_DEFAULT_MAX_TOKENS_SEGMENT: int = 2000


def _get_instructions_dir() -> Path:
    """通过 importlib.resources + 模块对象定位 instructions 包，不硬编码包名。"""
    ref = importlib.resources.files(_instructions_pkg)
    return Path(str(ref))


def _is_chinese_word(word: str) -> bool:
    """检查词元是否只含中文字符（不含英文字母）。与建库抽取器过滤口径一致。"""
    if not word or not word.strip():
        return False
    has_chinese = any("\u4e00" <= ch <= "\u9fff" for ch in word)
    has_english = any(ch.isalpha() and ord(ch) < 128 for ch in word)
    return has_chinese and not has_english


class QuerySegmenter:
    """
    查询中文分词器。

    仅负责把用户问句切分为词元列表（segmented_words），供 BM25 检索使用。
    分词 LLM 由调用方注入（内部固定 LLM，如 DeepSeek）。无 LLM 时 segment() 返回空列表，
    此时 BM25 检索会退化为不参与召回（向量检索不受影响）。
    """

    def __init__(
        self,
        llm_provider: Optional[RAGLLMProvider] = None,
        verbose: bool = True,
        instructions: Optional[str] = None,
    ):
        """
        Args:
            llm_provider: 用于中文分词的 RAGLLMProvider 实现（可选，无则返回空分词）。
            verbose: 是否输出详细日志。
            instructions: 外部传入的分词指令（可选）。传入则直接使用，不传则从内置 .md 加载。
        """
        self.verbose = verbose
        self.llm_provider = llm_provider
        if instructions:
            self._instructions = instructions
        else:
            instruction_file = _get_instructions_dir() / _SEGMENT_INSTRUCTION_FILENAME
            self._instructions = load_instruction(instruction_file)
        if self.verbose:
            if llm_provider is None:
                logger.info("QuerySegmenter 无 llm_provider，segment() 将返回空分词")
            else:
                logger.info("QuerySegmenter 初始化完成")

    async def segment(self, query: str) -> List[str]:
        """
        对用户问句做词元级中文分词。

        Args:
            query: 用户原始问句。

        Returns:
            分词结果（词元列表）；无 LLM 或调用失败时返回空列表。
        """
        if self.llm_provider is None:
            return []
        if not query or not query.strip():
            return []

        prompt = f'用户问句："{query.strip()}"'
        try:
            content = await self.llm_provider.run(
                prompt,
                self._instructions,
                agent_name=_AGENT_NAME_QUERY_SEGMENTER,
                max_tokens=_DEFAULT_MAX_TOKENS_SEGMENT,
            )
            content = (content or "").strip()
        except Exception as e:
            if self.verbose:
                logger.warning("分词 LLM 调用失败: %s", e)
            return []

        parsed = self._parse_response(content)
        segmented_raw = parsed.get(SchemaKeys.QA_SEGMENTED_WORDS, [])
        if not isinstance(segmented_raw, list):
            segmented_raw = []

        words = [
            str(w).strip()
            for w in segmented_raw
            if str(w).strip() and _is_chinese_word(str(w).strip())
        ]

        if self.verbose:
            logger.info("分词结果（%d 个词元）: %s", len(words), words)
        return words

    # ---------- 工具方法：从 LLM 输出中解析 JSON ----------

    def _parse_response(self, content: str) -> Dict[str, Any]:
        """从 LLM 输出中提取并解析 JSON，返回 dict（失败返回空 dict）。"""
        json_str = self._extract_json_object(content)
        if not json_str:
            return {}
        try:
            data = json.loads(json_str)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as e:
            if self.verbose:
                logger.warning("分词 JSON 解析失败: %s", e)
            return {}

    @staticmethod
    def _extract_json_object(text: str) -> Optional[str]:
        """从可能带 markdown 代码块的文本中提取 JSON 对象字符串。"""
        code_block = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if code_block:
            text = code_block.group(1)
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
