#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文本处理工具模块

提供文本处理相关的工具函数，包括：
1. NER实体提取（使用HanLP）
2. Token估算
"""

import gc
import json
import re
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

import logging

logger = logging.getLogger(__name__)

# ==================== 常量定义 ====================
# Token估算常量
CHINESE_CHAR_PER_TOKEN = 1.5
ENGLISH_CHAR_PER_TOKEN = 4.0

# Unicode范围定义
CHINESE_UNICODE_RANGES = [
    ('\u4e00', '\u9fff'),  # CJK统一表意文字
    ('\u3000', '\u303f'),  # CJK符号和标点
    ('\uff00', '\uffef'),  # 全角ASCII
]

# HanLP模型名称列表
HANLP_TOKENIZER_NAMES = [
    'hanlp.pretrained.tok.COARSE_ELECTRA_SMALL_ZH',
    'COARSE_ELECTRA_SMALL_ZH'
]

HANLP_NER_MODEL_NAME = 'MSRA_NER_ELECTRA_SMALL_ZH'

# 中文字符正则（性能优于逐范围遍历）
_CHINESE_CHAR_RE = re.compile(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]')

# ==================== NER模型相关 ====================
# NER 模型全局状态锁（保证线程安全）
_ner_lock = threading.Lock()

# 尝试导入HanLP
_hanlp_available = False
_ner_model = None
_ner_tokenizer = None

try:
    import hanlp  # noqa: F401  # type: ignore[import]  # 仅用于探测 hanlp 可用性
    _hanlp_available = True
except ImportError:
    _hanlp_available = False
except Exception as e:
    logger.warning("HanLP导入失败: %s", e)
    _hanlp_available = False

# 导出常量
HANLP_AVAILABLE = _hanlp_available


def unload_ner_model() -> None:
    """
    卸载NER模型，释放内存
    
    用于在不再需要NER功能时释放模型占用的内存。
    线程安全。
    """
    global _ner_model, _ner_tokenizer
    with _ner_lock:
        _ner_model = None
        _ner_tokenizer = None
    gc.collect()
    logger.info("NER模型已卸载，内存已释放")


def _load_tokenizer() -> Optional[Any]:
    """
    加载HanLP分词器

    .. note:: 必须在 ``_ner_lock`` 持有时调用，否则会引发 AssertionError。

    Returns:
        分词器实例，如果加载失败返回None
    """
    assert _ner_lock.locked(), "_load_tokenizer must be called with _ner_lock held"
    if not HANLP_AVAILABLE:
        return None
    
    import hanlp  # type: ignore[import]
    
    # 尝试多个可能的模型名称
    for tokenizer_name in HANLP_TOKENIZER_NAMES:
        try:
            # 尝试通过属性访问
            if '.' in tokenizer_name:
                parts = tokenizer_name.split('.')
                obj: Any = hanlp
                for part in parts:
                    obj = getattr(obj, part)
                return hanlp.load(str(obj))
            else:
                # 直接使用字符串加载
                return hanlp.load(tokenizer_name)
        except (AttributeError, ImportError, RuntimeError, FileNotFoundError) as e:
            logger.debug("尝试加载分词器 %s 失败: %s", tokenizer_name, e)
            continue
    
    logger.warning("分词器加载失败，NER可能无法正常工作")
    return None


def _find_local_ner_model() -> Optional[str]:
    """
    查找本地缓存的NER模型路径
    
    Returns:
        本地模型路径，如果未找到返回None
    """
    import os
    from pathlib import Path
    
    if sys.platform == 'win32':
        hanlp_dir = Path(os.environ.get('APPDATA', '')) / 'hanlp'
    else:
        hanlp_dir = Path.home() / '.hanlp'
    
    if not hanlp_dir.exists():
        return None
    
    ner_dir = hanlp_dir / 'ner'
    if not ner_dir.exists():
        return None
    
    for model_dir in ner_dir.iterdir():
        if not model_dir.is_dir():
            continue
        
        model_dir_name = model_dir.name.lower()
        if 'electra' in model_dir_name and 'msra' in model_dir_name:
            config_file = model_dir / 'config.json'
            if config_file.exists():
                return str(model_dir)
    
    return None


def load_ner_model(model_name: Optional[str] = None) -> Tuple[Optional[Any], Optional[Any]]:
    """
    加载HanLP NER模型和分词器
    
    Args:
        model_name: 模型名称，默认使用 MSRA_NER_ELECTRA_SMALL_ZH
    
    Returns:
        (ner_model, tokenizer) 元组，如果加载失败返回 (None, None)
    """
    global _ner_model, _ner_tokenizer

    if not HANLP_AVAILABLE:
        return None, None

    with _ner_lock:
        # 如果已经加载，直接返回（双重检查锁定）
        if _ner_model is not None and _ner_tokenizer is not None:
            return _ner_model, _ner_tokenizer

        try:
            import hanlp  # type: ignore[import]
            
            # 加载分词器
            _ner_tokenizer = _load_tokenizer()

            # 加载NER模型
            model_name = model_name or HANLP_NER_MODEL_NAME

            # 尝试查找本地模型
            local_path = _find_local_ner_model()

            if local_path:
                try:
                    import os
                    _ner_model = hanlp.load(local_path)
                    logger.info("NER模型加载成功: %s", os.path.basename(local_path))
                except Exception as e:
                    logger.warning("本地模型加载失败: %s，尝试使用预训练模型", e)
                    _ner_model = hanlp.load(model_name)
            else:
                _ner_model = hanlp.load(model_name)

            logger.info("NER模型和分词器加载成功")
            return _ner_model, _ner_tokenizer

        except Exception as e:
            logger.warning("NER模型加载失败: %s", e)
            _ner_model = None
            _ner_tokenizer = None
            return None, None


def _calculate_char_position(
    text: str, 
    word: str, 
    start_idx: int, 
    end_idx: int, 
    words_list: Optional[List[str]]
) -> Tuple[int, int]:
    """
    计算实体在原文中的字符位置
    
    Args:
        text: 原始文本
        word: 实体文本
        start_idx: 词级别的开始索引
        end_idx: 词级别的结束索引
        words_list: 分词后的词列表
    
    Returns:
        (start, end) 字符位置元组
    """
    if words_list:
        char_pos = 0
        start = 0
        end = 0
        for i, w in enumerate(words_list):
            if i == start_idx:
                start = char_pos
            if i == end_idx:
                end = char_pos + len(w)
                break
            char_pos += len(w)
        return start, end
    else:
        start = text.find(word)
        if start == -1:
            logger.warning("实体 '%s' 在原文中未找到", word)
            return 0, 0
        return start, start + len(word)


def _normalize_entity_label(label: str) -> str:
    """
    标准化实体标签
    
    Args:
        label: 原始标签
    
    Returns:
        标准化后的标签（PER/LOC/ORG等）
    """
    label_normalized = label.upper()
    if 'PERSON' in label_normalized or label_normalized == 'PER':
        return 'PER'
    elif 'LOCATION' in label_normalized or label_normalized == 'LOC':
        return 'LOC'
    elif 'ORGANIZATION' in label_normalized or label_normalized == 'ORG':
        return 'ORG'
    return label_normalized


def extract_entities_by_hanlp(
    text: str, 
    ner_model: Optional[Any] = None, 
    tokenizer: Optional[Any] = None, 
    verbose: bool = False
) -> List[Dict[str, Any]]:
    """
    使用HanLP从文本中提取命名实体
    
    Args:
        text: 输入文本
        ner_model: NER模型（如果为None，会自动加载）
        tokenizer: 分词器（如果为None，会自动加载）
        verbose: 是否输出详细信息
    
    Returns:
        实体列表，每个实体包含 text, label, start, end
    """
    if not text or not text.strip():
        if verbose:
            logger.warning("输入文本为空")
        return []
    
    if not HANLP_AVAILABLE:
        if verbose:
            logger.warning("HanLP不可用，无法提取实体")
        return []

    # 如果没有提供模型，尝试加载
    if ner_model is None or tokenizer is None:
        ner_model, tokenizer = load_ner_model()
        if ner_model is None:
            if verbose:
                logger.warning("NER模型未加载，无法提取实体")
            return []

    try:
        # 分词（只调用一次）
        words_list = None
        if tokenizer is not None:
            words_list = tokenizer(text)
            # NER模型需要输入分词后的词列表
            result = ner_model([words_list])
        else:
            # 尝试直接使用文本
            result = ner_model([text])

        # 解析结果
        entities: List[Dict[str, Any]] = []

        if not isinstance(result, list) or len(result) == 0:
            return []
        
        text_result = result[0]

        if isinstance(text_result, list):
            for item in text_result:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                
                word = item[0]
                label = item[1]

                # 获取位置信息
                if len(item) >= 4:
                    start_idx = item[2]
                    end_idx = item[3]
                    start, end = _calculate_char_position(text, word, start_idx, end_idx, words_list)
                else:
                    start, end = _calculate_char_position(text, word, 0, 0, None)

                # 标准化标签
                label_normalized = _normalize_entity_label(label)

                entities.append({
                    'text': word,
                    'label': label_normalized,
                    'start': start,
                    'end': end
                })

        if verbose:
            logger.info("NER提取到 %s 个实体: %s", len(entities), [(e['text'], e['label']) for e in entities])

        return entities

    except Exception as e:
        if verbose:
            logger.warning("NER提取失败: %s", e)
        return []


def _is_chinese_char(char: str) -> bool:
    """
    判断字符是否为中文字符（包括中文标点）
    
    Args:
        char: 单个字符
    
    Returns:
        如果是中文字符返回True，否则返回False
    """
    return bool(_CHINESE_CHAR_RE.match(char))


def estimate_tokens(text: str, tokenizer: Optional[Any] = None) -> int:
    """
    估算文本的 token 数量

    当提供 ``tokenizer`` 时使用精确计算（调用 ``tokenizer.encode(text)``），
    否则使用基于字符数的估算规则：

    - 中文字符：1 token ≈ 1.5 字符
    - 英文/数字：1 token ≈ 4 字符

    Args:
        text: 输入文本
        tokenizer: 可选的精确分词器对象（如 tiktoken.Encoding），
            需实现 ``encode(text) -> list`` 接口。

    Returns:
        估算的 token 数量，空文本或仅包含空白字符返回0，非空文本至少返回1
    """
    if not text or not text.strip():
        return 0

    # 精确模式：使用外部 tokenizer
    if tokenizer is not None:
        try:
            return max(1, len(tokenizer.encode(text)))
        except Exception:
            pass  # fallback 到估算模式

    # 统计中文字符数量（包括中文标点）
    chinese_chars = sum(1 for char in text if _is_chinese_char(char))

    # 其他字符（英文、数字、空格等）
    other_chars = len(text) - chinese_chars

    # 估算：中文字符按 1.5 字符/token，其他字符按 4 字符/token
    estimated_tokens = int(
        chinese_chars / CHINESE_CHAR_PER_TOKEN + 
        other_chars / ENGLISH_CHAR_PER_TOKEN
    )

    # 至少返回 1（如果文本不为空）
    return max(1, estimated_tokens)


def generate_chunk_title(metadata: Dict[str, Any], document: str, max_preview: int = 15) -> str:
    """
    根据元数据和文档内容生成标题
    
    标题生成规则：
    1. 如果有章节信息，则：章节信息 + 子文档前N个字
    2. 如果有场景信息，则：场景信息 + 子文档前N个字
    3. 否则：子文档前N个字
    
    Args:
        metadata: 文档元数据（包含 chapter_title, chapter_index, scene_title, scene_index 等字段）
        document: 文档内容
        max_preview: 内容预览的最大字符数（默认15）
    
    Returns:
        生成的标题字符串
    """
    title_parts: List[str] = []

    # 1. 章节信息
    chapter_title = metadata.get('chapter_title')
    if chapter_title:
        chapter_idx = metadata.get('chapter_index')
        if chapter_idx is not None:
            title_parts.append(f"第{chapter_idx}章: {chapter_title}")
        else:
            title_parts.append(chapter_title)
    elif metadata.get('chapter_index') is not None:
        title_parts.append(f"第{metadata.get('chapter_index')}章")

    # 2. 场景信息（如果有场景，优先显示场景）
    scene_title = metadata.get('scene_title')
    if scene_title:
        scene_idx = metadata.get('scene_index')
        if scene_idx is not None:
            title_parts.append(f"场景{scene_idx}: {scene_title}")
        else:
            title_parts.append(f"场景: {scene_title}")
    elif metadata.get('scene_index') is not None:
        title_parts.append(f"场景{metadata.get('scene_index')}")

    # 3. 子文档前N个字
    content_preview = document[:max_preview].strip().replace('\n', ' ').replace('\t', ' ') if document else ""

    # 组合标题
    if title_parts:
        generated_title = " ".join(title_parts) + " " + content_preview
    else:
        generated_title = content_preview

    return generated_title


def normalize_list_field(
    value: Any, 
    verbose: bool = False, 
    field_name: str = "field"
) -> List[str]:
    """
    将值标准化为字符串列表
    
    支持的输入格式：
    1. List[str] - 直接返回（标准格式）
    2. JSON字符串 - 解析后返回列表
    3. 其他格式 - 返回空列表
    
    Args:
        value: 输入值（可能是列表、JSON字符串或其他类型）
        verbose: 是否输出警告信息
        field_name: 字段名称（用于日志输出）
    
    Returns:
        标准化后的字符串列表
        
    Note:
        value 类型不支持时，仅在 verbose=True 时通过日志警告，不会 raise 异常。
    """
    if value is None:
        return []
    
    if isinstance(value, list):
        # 直接是列表格式（标准格式）
        return [str(x).strip() for x in value if x is not None and str(x).strip()]
    
    if isinstance(value, str):
        # JSON 字符串格式
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if x is not None and str(x).strip()]
            else:
                if verbose:
                    logger.warning(
                        "%s JSON解析结果不是列表，而是 %s",
                        field_name, type(parsed).__name__,
                    )
                return []
        except json.JSONDecodeError as e:
            # 如果不是有效的 JSON，返回空列表
            if verbose:
                logger.warning(
                    "%s JSON解析失败: %s, 实际值: %s...",
                    field_name, e,
                    value[:50] if len(value) > 50 else value,
                )
            return []
    
    if verbose:
        logger.warning(
            "%s 类型不支持: %s, 期望 List[str] 或 JSON 字符串",
            field_name, type(value).__name__,
        )
    
    return []
