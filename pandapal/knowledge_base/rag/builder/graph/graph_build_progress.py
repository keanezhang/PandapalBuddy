#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图谱构建进度（断点续传）

委托给 builder.build_progress 公共模块，仅绑定 PROGRESS_DIR_NAME。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from ..build_progress import (
    PROGRESS_VERSION,
    progress_file_path as _progress_file_path,
    load_processed_parent_ids as _load,
    save_progress as _save,
    clear_progress as _clear,
)

PROGRESS_DIR_NAME = "_graph_build_progress"


def progress_file_path(db_path: Path, collection_name: str) -> Path:
    """进度文件路径"""
    return _progress_file_path(db_path, collection_name, PROGRESS_DIR_NAME)


def load_processed_parent_ids(db_path: Path, collection_name: str) -> List[str]:
    """加载已处理的 parent_id 列表"""
    return _load(db_path, collection_name, PROGRESS_DIR_NAME)


def save_progress(
    db_path: Path,
    collection_name: str,
    processed_parent_ids: List[str],
) -> None:
    """保存进度"""
    _save(db_path, collection_name, processed_parent_ids, PROGRESS_DIR_NAME)


def clear_progress(db_path: Path, collection_name: str) -> None:
    """清除进度文件"""
    _clear(db_path, collection_name, PROGRESS_DIR_NAME)
