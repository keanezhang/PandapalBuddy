#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建进度（断点续传）— 公共模块

按 parent_id 记录已处理的批次，下次启动时只处理未完成的批次。
进度文件存放在 db_path / "{progress_dir_name}" / "{collection_name}.json"。

graph_build_progress 和 vector_build_progress 共用此实现，仅 progress_dir_name 不同。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

PROGRESS_VERSION = 1


def _progress_dir(db_path: Path, dir_name: str) -> Path:
    """进度目录：db_path / dir_name"""
    return db_path / dir_name


def progress_file_path(db_path: Path, collection_name: str, dir_name: str) -> Path:
    """进度文件路径"""
    return _progress_dir(db_path, dir_name) / f"{collection_name}.json"


def load_processed_parent_ids(
    db_path: Path, collection_name: str, dir_name: str
) -> List[str]:
    """
    加载已处理的 parent_id 列表（断点续传用，顺序与写入时一致）。

    Args:
        db_path: 数据库目录
        collection_name: 集合名称
        dir_name: 进度目录名称

    Returns:
        已处理的 parent_id 列表；文件不存在或无效时返回空列表
    """
    path = progress_file_path(db_path, collection_name, dir_name)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if data.get("version") != PROGRESS_VERSION or data.get("collection_name") != collection_name:
            return []
        ids = data.get("processed_parent_ids") or []
        return list(ids)
    except Exception as e:
        logger.warning("读取构建进度失败，将从头构建: %s", e)
        return []


def save_progress(
    db_path: Path,
    collection_name: str,
    processed_parent_ids: List[str],
    dir_name: str,
) -> None:
    """
    保存进度：将已处理的 parent_id 列表写入文件。

    Args:
        db_path: 数据库目录
        collection_name: 集合名称
        processed_parent_ids: 已处理的 parent_id 列表（可含重复，会去重写入）
        dir_name: 进度目录名称
    """
    path = progress_file_path(db_path, collection_name, dir_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    unique = list(dict.fromkeys(processed_parent_ids))  # 保持顺序、去重
    data = {
        "version": PROGRESS_VERSION,
        "collection_name": collection_name,
        "processed_parent_ids": unique,
    }
    content = json.dumps(data, ensure_ascii=False, indent=2)
    # 原子写入：先写临时文件再 os.replace，避免写入中断导致文件损坏
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def clear_progress(db_path: Path, collection_name: str, dir_name: str) -> None:
    """
    清除进度文件（rebuild 时调用）。
    """
    path = progress_file_path(db_path, collection_name, dir_name)
    if path.exists():
        path.unlink()
        logger.info("已清除构建进度: %s", path)
