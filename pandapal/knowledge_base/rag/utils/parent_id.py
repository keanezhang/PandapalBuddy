#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Parent ID 工具函数模块

提供 parent_id 相关的工具函数，用于构建、解析和验证 parent_id。
parent_id 用于标识文档的父子关系，格式：
- 章节级别: "parent_chapter_{chapter_index}"
- 场景级别: "parent_chapter_{chapter_index}_scene_{scene_index}"
"""

import re
from typing import Optional, Tuple

# ==================== 正则表达式常量 ====================
# 使用 $ 锚点确保精确匹配
PATTERN_CHAPTER_WITH_SCENE = re.compile(r"^parent_chapter_(\d+)_scene_(\d+)$")
PATTERN_CHAPTER_ONLY = re.compile(r"^parent_chapter_(\d+)$")


def build_parent_id(chapter_index: int, scene_index: Optional[int] = None) -> str:
    """
    构建parent_id，确保格式一致
    
    用于双路召回时，确保图谱生成的parent_id与文档分割时生成的格式完全一致。
    
    Args:
        chapter_index: 章节索引（正整数，从1开始）
        scene_index: 场景索引（正整数，从1开始，可选）
        
    Returns:
        parent_id字符串，格式：
        - 如果只有章节: "parent_chapter_{chapter_index}"
        - 如果有场景: "parent_chapter_{chapter_index}_scene_{scene_index}"
        
    Raises:
        ValueError: 如果 chapter_index 不是正整数，或 scene_index 不是正整数
        
    Examples:
        >>> build_parent_id(1)
        'parent_chapter_1'
        >>> build_parent_id(2, 3)
        'parent_chapter_2_scene_3'
    """
    if not isinstance(chapter_index, int) or chapter_index < 1:
        raise ValueError(f"chapter_index 必须是正整数，得到: {chapter_index!r}")
    if scene_index is not None and (not isinstance(scene_index, int) or scene_index < 1):
        raise ValueError(f"scene_index 必须是正整数或 None，得到: {scene_index!r}")
    if scene_index is not None:
        return f"parent_chapter_{chapter_index}_scene_{scene_index}"
    return f"parent_chapter_{chapter_index}"


def parse_parent_id(parent_id: str) -> Tuple[Optional[int], Optional[int]]:
    """
    解析parent_id，返回章节和场景索引
    
    Args:
        parent_id: parent_id字符串
        
    Returns:
        (chapter_index, scene_index) 元组
        - chapter_index: 章节索引（从1开始），如果解析失败返回None
        - scene_index: 场景索引（从1开始），如果没有场景或解析失败返回None
        
    Examples:
        >>> parse_parent_id("parent_chapter_1")
        (1, None)
        >>> parse_parent_id("parent_chapter_2_scene_3")
        (2, 3)
        >>> parse_parent_id("invalid_id")
        (None, None)
    """
    if not parent_id:
        return None, None
    
    # 先匹配格式: parent_chapter_{chapter_index}_scene_{scene_index}
    match_with_scene = PATTERN_CHAPTER_WITH_SCENE.match(parent_id)
    if match_with_scene:
        return int(match_with_scene.group(1)), int(match_with_scene.group(2))

    # 再匹配格式: parent_chapter_{chapter_index}
    match_chapter_only = PATTERN_CHAPTER_ONLY.match(parent_id)
    if match_chapter_only:
        return int(match_chapter_only.group(1)), None

    # 解析失败
    return None, None


def validate_parent_id_format(parent_id: str) -> bool:
    """
    验证parent_id格式是否正确
    
    Args:
        parent_id: parent_id字符串
        
    Returns:
        如果格式正确返回True，否则返回False
    """
    chapter_index, _scene_index = parse_parent_id(parent_id)
    return chapter_index is not None
