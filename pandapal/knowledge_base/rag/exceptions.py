#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG SDK 统一异常体系。

所有 SDK 异常均继承自 RAGError，调用方可通过 ``except RAGError`` 捕获所有 SDK 异常。

异常层次：
    RAGError
    ├── RAGConfigError        # 配置相关错误（参数校验失败、缺少必填字段等）
    ├── RAGIndexError         # 索引相关错误（初始化失败、索引为空等）
    ├── RAGQueryError         # 查询相关错误（意图分类器未配置、query 执行失败等）
    ├── RAGBuildError         # 构建流水线错误（数据加载、分块、向量化失败等）
    └── RAGComponentError     # 组件初始化/运行错误（Embedding、Reranker、GraphStore 等）
"""

from __future__ import annotations


class RAGError(Exception):
    """RAG SDK 所有异常的基类。"""


class RAGConfigError(RAGError):
    """配置相关错误：参数校验失败、缺少必填字段、类型不匹配等。"""


class RAGIndexError(RAGError):
    """索引相关错误：初始化失败、索引为空、加载失败等。"""


class RAGQueryError(RAGError):
    """查询相关错误：意图分类器未配置、查询执行失败、LLM 调用失败等。"""


class RAGBuildError(RAGError):
    """构建流水线错误：数据加载、分块、向量化、图谱构建失败等。"""


class RAGComponentError(RAGError):
    """组件初始化/运行错误：Embedding、Reranker、GraphStore 等组件的创建或调用失败。"""
