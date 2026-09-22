#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重排序模块

负责对检索结果进行重排序，提高相关性。
支持：本地 FlagReranker、云端重排 API（显式 api_key+model_id）。
"""

import importlib.resources
import json
from pathlib import Path
from typing import Any, List, Literal, Optional

import logging

from ...instructions.instruction_loader import load_instruction
from ... import instructions as _instructions_pkg  # 模块对象引用，零包名耦合
from ...schema import ChildDocument
from ..retrieval.search_constants import DASHSCOPE_RERANK_URL

# ---------- numpy: 可选延迟导入 ----------
_np = None  # type: Any

def _ensure_numpy() -> Any:
    global _np
    if _np is None:
        import numpy as np
        _np = np
    return _np

# ---------- requests/urllib3: 延迟导入 ----------
_requests = None  # type: Any
_HTTPAdapter = None  # type: Any
_Retry = None  # type: Any

def _ensure_requests() -> Any:
    global _requests, _HTTPAdapter, _Retry
    if _requests is None:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        _requests = requests
        _HTTPAdapter = HTTPAdapter
        _Retry = Retry
    return _requests

# ---------- 可选依赖：本地重排序 ----------
try:
    from FlagEmbedding import FlagReranker  # type: ignore[import]
    RERANKER_SUPPORT = True
except ImportError:
    RERANKER_SUPPORT = False
    FlagReranker = None

logger = logging.getLogger(__name__)

__all__ = ["Reranker", "RERANKER_SUPPORT"]

# ---------- HTTP 重试会话（复用单例） ----------
_session = None  # type: Optional[Any]


def _get_retry_session() -> Any:
    """获取带重试策略的 requests.Session（复用单例）。"""
    global _session
    if _session is None:
        _ensure_requests()
        retry_strategy = _Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["POST"],
        )
        _session = _requests.Session()
        adapter = _HTTPAdapter(max_retries=retry_strategy)
        _session.mount("https://", adapter)
        _session.mount("http://", adapter)
    return _session

# ---------- 常量 ----------
CLOUD_RERANK_BUSINESS_QWEN3 = "qwen3-rerank"
RERANKER_CLOUD_INSTRUCT_ID = "reranker_cloud_instruct"

def _get_reranker_cloud_instruct_file() -> Path:
    """通过 importlib.resources + 模块对象定位 instruction 文件，不硬编码包名。"""
    ref = importlib.resources.files(_instructions_pkg)
    return Path(str(ref)) / "reranker_cloud_instruct.md"

# 云端 API 单条最大约 4000 tokens，用字符截断做保守估计
CLOUD_RERANK_MAX_CONTENT_CHARS = 5000


# ---------- 重排序器类 ----------


class Reranker:
    """
    重排序器

    支持两种后端：
    - 本地：model_path → FlagReranker
    - 云端 API：cloud_api_key + cloud_model_id（可选 cloud_url、cloud_instruct）
    """

    def __init__(
        self,
        model_path: Optional[Path] = None,
        *,
        cloud_api_key: Optional[str] = None,
        cloud_model_id: Optional[str] = None,
        cloud_url: Optional[str] = None,
        cloud_instruct: Optional[str] = None,
        verbose: bool = True,
    ):
        """
        初始化重排序器。二选一：model_path（本地）、或 cloud_api_key+cloud_model_id（云端 API）。
        """
        has_local = model_path is not None
        has_cloud = bool(cloud_api_key and cloud_model_id)
        n = sum([has_local, has_cloud])
        if n != 1:
            raise ValueError(
                "请仅提供其一：model_path（本地）、或 cloud_api_key+cloud_model_id（云端 API）。"
            )
        self.model_path = Path(model_path) if model_path else None
        self.verbose = verbose
        self._backend: Literal["local", "cloud"] = "cloud" if has_cloud else "local"
        self._local_reranker = None
        self._cloud_config = None
        self._cloud_instruct = None
        if has_cloud:
            rerank_url = (cloud_url or DASHSCOPE_RERANK_URL).strip()
            if not rerank_url:
                raise ValueError(
                    "cloud_url is required for cloud reranking. "
                    "请通过 cloud_url 参数或 DASHSCOPE_RERANK_URL 常量配置重排序 API 地址。"
                )
            self._cloud_config = {
                "api_key": cloud_api_key,
                "model_id": cloud_model_id,
                "rerank_url": rerank_url,
            }
            self._cloud_instruct = (
                cloud_instruct.strip() if cloud_instruct and cloud_instruct.strip()
                else self._get_cloud_instruct()
            )
            if verbose:
                logger.info(" 使用云端重排序（显式配置）: %s", self._cloud_config.get("model_id", ""))
        else:
            self._init_local()

    def __repr__(self) -> str:
        """脱敏表示，避免序列化/打印时泄露 API Key。"""
        return (
            f"Reranker(backend={self._backend!r}, "
            f"model_path={self.model_path!r}, "
            f"cloud_model_id={self._cloud_config.get('model_id') if self._cloud_config else None!r})"
        )

    # ---------- 云端：指令与重排序 ----------

    def _get_cloud_instruct(self) -> Optional[str]:
        """
        获取云端重排序任务指令（instruct）。
        从 RAG instructions 目录加载 .md 文件。
        """
        instruct_file = _get_reranker_cloud_instruct_file()
        if not instruct_file.exists():
            return None
        try:
            text = load_instruction(instruct_file)
        except Exception:
            return None
        return text.strip() if (text and isinstance(text, str)) else None

    def _rerank_cloud(
        self,
        query: str,
        documents: List[ChildDocument],
        top_k: Optional[int] = None,
    ) -> List[ChildDocument]:
        """
        使用云端千问重排序 API（Dashscope qwen3-rerank）对文档重排序。

        参数来源：model / api_key / rerank_url 由构造时显式传入；instruct 由构造时 cloud_instruct 或 instruction 文件加载。
        """
        cfg = self._cloud_config
        if not cfg:
            raise RuntimeError("云端重排序配置未初始化")
        texts = []
        for doc in documents:
            content = doc.get("content", "") or ""
            if len(content) > CLOUD_RERANK_MAX_CONTENT_CHARS:
                content = content[:CLOUD_RERANK_MAX_CONTENT_CHARS]
            texts.append(content)
        top_n_val = top_k if top_k is not None and top_k > 0 else len(texts)
        payload = {
            "model": cfg["model_id"],
            "query": query,
            "documents": texts,
            "top_n": top_n_val,
        }
        if self._cloud_instruct:
            payload["instruct"] = self._cloud_instruct
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }
        if self.verbose:
            logger.info(
                " 使用云端重排序 (model=%s) 对 %d 个文档重排序，top_n=%s，instruct=%s",
                cfg.get("model_id", ""),
                len(texts),
                top_n_val,
                "已设置" if self._cloud_instruct else "未设置",
            )
        try:
            session = _get_retry_session()
            resp = session.post(
                cfg["rerank_url"],
                headers=headers,
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
        except _requests.RequestException as e:
            # 打印响应体帮助定位问题
            resp_body = ""
            if hasattr(e, "response") and e.response is not None:
                try:
                    resp_body = e.response.text
                except Exception:
                    pass
            logger.error(" 云端重排序请求失败: %s | 响应体: %s", e, resp_body, exc_info=True)
            return documents
        except ValueError as e:
            logger.error(" 云端重排序响应解析失败: %s", e, exc_info=True)
            return documents
        if self.verbose:
            logger.debug(" 云端重排序 API 返回: %s", json.dumps(data, ensure_ascii=False, indent=2))
        # 兼容两种响应格式：顶层 data["results"]（如 Dashscope）或 data["output"]["results"]
        results = data.get("results") or (data.get("output") or {}).get("results") or []
        # results: [ {"index": 0, "relevance_score": 0.93, ... }, ... ] 按分数从高到低
        index_to_score = {r["index"]: float(r.get("relevance_score", 0.0)) for r in results}
        reranked_docs = []
        for i, r in enumerate(results):
            idx = r["index"]
            if idx < 0 or idx >= len(documents):
                continue
            doc = documents[idx].copy()
            doc["rerank_score"] = index_to_score.get(idx, 0.0)
            doc["rerank_original_rank"] = idx + 1
            doc["rerank_final_rank"] = i + 1
            reranked_docs.append(doc)
        # 未出现在 results 里的文档补在末尾（理论上 API 会返回全部，此处做兜底）
        for idx in range(len(documents)):
            if idx not in index_to_score:
                doc = documents[idx].copy()
                doc["rerank_score"] = 0.0
                doc["rerank_original_rank"] = idx + 1
                doc["rerank_final_rank"] = len(reranked_docs) + 1
                reranked_docs.append(doc)
        if self.verbose:
            top_scores = [d.get("rerank_score", 0.0) for d in reranked_docs[:5]]
            logger.info(" 云端重排序完成，前 5 个文档分数: %s", top_scores)
        if top_k is not None and top_k > 0:
            return reranked_docs[:top_k]
        return reranked_docs

    # ---------- 本地模型：加载与重排序 ----------

    def _init_local(self) -> None:
        self._local_reranker = self.load_local_model()

    def load_local_model(self) -> Optional["FlagReranker"]:
        """
        加载本地 FlagReranker 模型。仅本地模式可用，已加载则返回缓存实例。
        """
        if self._backend != "local" or self.model_path is None:
            raise RuntimeError("仅本地模式可加载模型")
        if not RERANKER_SUPPORT:
            raise ImportError(
                "重排序器功能需要安装 FlagEmbedding 库。"
                "安装方法: pip install FlagEmbedding"
            )
        if self._local_reranker is not None:
            return self._local_reranker
        model_path = self.model_path
        if not model_path.exists():
            raise FileNotFoundError(
                f"本地模型路径不存在: {model_path}\n请确保模型文件已下载到该路径"
            )
        config_file = model_path / "config.json"
        if not config_file.exists():
            raise FileNotFoundError(
                f"模型配置文件不存在: {config_file}\n请检查模型目录是否完整"
            )
        try:
            if self.verbose:
                logger.info(" 从本地加载重排序器模型: %s", model_path)
            abs_model_path = str(model_path.resolve())
            self._local_reranker = self._create_local_reranker(abs_model_path)
        except Exception as e:
            logger.error(" 本地模型加载失败: %s", e)
            raise RuntimeError(
                f"无法加载本地重排序器模型 '{model_path}'。\n错误详情: {e}"
            ) from e
        return self._local_reranker

    def _create_local_reranker(self, model_path: str) -> Any:
        """创建本地 FlagReranker 实例（先尝试 use_fp16，再回退默认）。"""
        try:
            return FlagReranker(model_path, use_fp16=True)
        except Exception as e:
            logger.debug("fp16 初始化失败，回退默认模式: %s", e)
        try:
            return FlagReranker(model_path)
        except Exception as e:
            raise RuntimeError(f"无法初始化 FlagReranker: {e}") from e

    def _rerank_local(
        self,
        query: str,
        documents: List[ChildDocument],
        top_k: Optional[int] = None,
    ) -> List[ChildDocument]:
        """使用本地 FlagReranker 对文档重排序。"""
        if not RERANKER_SUPPORT:
            if self.verbose:
                logger.warning(
                    " 重排序器未安装，跳过重排序。安装方法: pip install FlagEmbedding"
                )
            return documents
        reranker = self._local_reranker
        if reranker is None:
            if self.verbose:
                logger.warning(" 本地重排序器未加载，跳过重排序")
            return documents
        pairs = []
        for doc in documents:
            content = doc.get("content", "") or ""
            pairs.append([query, content])
        if self.verbose:
            logger.info(" 使用本地重排序器对 %d 个文档进行重排序...", len(pairs))
        try:
            scores = reranker.compute_score(pairs, normalize=True)
        except Exception as e:
            logger.error(" 本地重排序失败: %s", e, exc_info=True)
            return documents
        if isinstance(scores, (int, float)):
            scores = [float(scores)]
        elif isinstance(scores, _ensure_numpy().ndarray):
            scores = scores.tolist()
        elif isinstance(scores, list):
            scores = [float(s) for s in scores]
        else:
            try:
                scores = [float(scores)]
            except (TypeError, ValueError):
                logger.error(" 无法处理重排序分数格式: %s", type(scores))
                return documents
        reranked_docs = []
        for i, doc in enumerate(documents):
            reranked_doc = doc.copy()
            reranked_doc["rerank_score"] = float(scores[i]) if i < len(scores) else 0.0
            reranked_doc["rerank_original_rank"] = i + 1
            reranked_docs.append(reranked_doc)
        reranked_docs.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
        for i, doc in enumerate(reranked_docs):
            doc["rerank_final_rank"] = i + 1
        if self.verbose:
            top_scores = [d.get("rerank_score", 0.0) for d in reranked_docs[:5]]
            logger.info(" 本地重排序完成，前 5 个文档分数: %s", top_scores)
        if top_k is not None and top_k > 0:
            return reranked_docs[:top_k]
        return reranked_docs

    # ---------- 对外接口 ----------

    def rerank(
        self,
        query: str,
        documents: List[ChildDocument],
        top_k: Optional[int] = None,
    ) -> List[ChildDocument]:
        """
        对文档进行重排序。根据初始化时的 backend 调用云端或本地重排序。
        """
        if not documents:
            return documents
        if self._backend == "cloud":
            return self._rerank_cloud(query, documents, top_k)
        return self._rerank_local(query, documents, top_k)
