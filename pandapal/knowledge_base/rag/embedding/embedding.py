#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
向量化模块

负责将文本转换为向量，支持：
1. 本地模型向量化（使用 sentence-transformers）
2. 云端 API 向量化（使用火山引擎 Embedding API）
"""

import gc
import logging
from typing import List, Optional, Dict, Any, Union
from pathlib import Path
from dataclasses import dataclass

import numpy as np

from ..exceptions import RAGComponentError

logger = logging.getLogger(__name__)

# requests/urllib3 延迟导入（仅云端模式需要）
_requests = None
_HTTPAdapter = None
_Retry = None


def _ensure_requests() -> None:
    """确保 requests/urllib3 已导入（仅云端模式需要）。"""
    global _requests, _HTTPAdapter, _Retry
    if _requests is None:
        import requests as _req
        from requests.adapters import HTTPAdapter as _HA
        from urllib3.util.retry import Retry as _R
        _requests = _req
        _HTTPAdapter = _HA
        _Retry = _R

# 用于获取模型维度的测试文本（任意文本都可以，用于触发编码以获取维度信息）
_DIMENSION_TEST_TEXT = "dimension_test"

# 本地模型编码时是否显示进度条
_SHOW_PROGRESS_BAR = False  # Controlled via verbose parameter

# token 与字符的估算比例（1 token ≈ 1.5 个字符）
_TOKENS_TO_CHARS_RATIO = 1.5


class EmbeddingModelLoadError(RAGComponentError):
    """向量模型加载失败时抛出的自定义异常。"""


@dataclass(frozen=True)
class CloudEmbeddingConfig:
    """云端向量化配置（不可变）。"""
    api_key: str
    model: str
    api_type: str  # 默认 "multimodal"
    api_full_url: str
    dimension: Optional[int] = None

    def __post_init__(self):
        """验证配置参数"""
        # 类型检查 — 给出明确错误信息
        if not isinstance(self.api_key, str):
            raise RAGComponentError(
                f"api_key 必须是非空字符串，收到 {type(self.api_key).__name__}"
            )
        if not isinstance(self.model, str):
            raise RAGComponentError(
                f"model 必须是非空字符串，收到 {type(self.model).__name__}"
            )
        if not isinstance(self.api_type, str):
            raise RAGComponentError(
                f"api_type 必须是字符串，收到 {type(self.api_type).__name__}"
            )
        if not isinstance(self.api_full_url, str):
            raise RAGComponentError(
                f"api_full_url 必须是非空字符串，收到 {type(self.api_full_url).__name__}"
            )

        # frozen=True 时需要 object.__setattr__ 来修改
        object.__setattr__(self, 'api_key', self.api_key.strip())
        object.__setattr__(self, 'model', self.model.strip())
        object.__setattr__(self, 'api_type', self.api_type.lower().strip())
        object.__setattr__(self, 'api_full_url', self.api_full_url.strip())

        if not self.api_key:
            raise RAGComponentError("使用云端向量化时，api_key 参数必填（不能为空字符串）")
        if not self.model:
            raise RAGComponentError("使用云端向量化时，model 参数必填（不能为空字符串）")
        if self.api_type not in ("text", "multimodal"):
            raise RAGComponentError(
                f"api_type 必须是 'text' 或 'multimodal'，当前值: {self.api_type!r}"
            )
        if not self.api_full_url:
            raise RAGComponentError("api_full_url 不能为空")

    @property
    def supports_batch(self) -> bool:
        """是否支持批量处理（仅 text 类型支持）"""
        return self.api_type == "text"


@dataclass(frozen=True)
class LocalEmbeddingConfig:
    """本地向量化配置（不可变）。"""
    model_name: str
    model_path: Path

    def __post_init__(self):
        """初始化时自动校验模型名称和路径是否有效"""
        if not self.model_name:
            raise RAGComponentError("使用本地模型时，model_name 参数必填")
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"模型路径不存在: {self.model_path}\n"
                f"请确保模型已下载到该路径"
            )


class EmbeddingEncoder:
    """
    向量编码器

    支持本地和云端两种向量化方式。

    Example::

        # 云端模式
        encoder = EmbeddingEncoder.create_cloud(
            api_key="sk-xxx", model="emb-v1",
            api_type="text", api_full_url="https://api.example.com/emb",
        )
        vectors = encoder.encode_texts(["hello", "world"])

        # 本地模式
        encoder = EmbeddingEncoder.create_local(
            model_name="bge-base", model_path=Path("models/bge"),
        )
        with encoder:
            vectors = encoder.encode_texts(["hello"])
    """

    # 模型状态常量
    _STATE_NOT_LOADED = "not_loaded"
    _STATE_LOADED = "loaded"
    _STATE_UNLOADED = "unloaded"

    def __init__(
        self,
        cloud_config: Optional[CloudEmbeddingConfig] = None,
        local_config: Optional[LocalEmbeddingConfig] = None,
        verbose: bool = True,
        *,
        local_batch_size: int = 32,
        supported_dimensions: Optional[List[int]] = None,
        cloud_max_tokens_per_text: int = 4096,
        cloud_batch_size: int = 4,
        retry_total: int = 3,
        retry_backoff_factor: float = 0.5,
        retry_status_forcelist: Optional[List[int]] = None,
    ):
        """
        初始化向量编码器，cloud_config 和 local_config 至少提供一个，同时提供时优先使用云端。
        纯 SDK：batch/dimension 等由参数传入，不读 settings。

        Args:
            cloud_config: 云端向量化配置
            local_config: 本地向量化配置
            verbose: 是否输出详细信息
            local_batch_size: 本地模型批处理大小
            supported_dimensions: 支持的向量维度列表
            cloud_max_tokens_per_text: 云端 API 单条文本最大 token 数
            cloud_batch_size: 云端 API 批处理大小
            retry_total: HTTP 重试总次数
            retry_backoff_factor: HTTP 重试退避因子
            retry_status_forcelist: 需要重试的 HTTP 状态码列表
        """
        if cloud_config is None and local_config is None:
            raise RAGComponentError("必须提供 cloud_config 或 local_config 之一")

        self.use_cloud_embedding = cloud_config is not None
        self.verbose = verbose
        self._local_batch_size = local_batch_size
        self._supported_dimensions = list(supported_dimensions) if supported_dimensions is not None else [1024, 2048]
        self._cloud_max_tokens_per_text = cloud_max_tokens_per_text
        self._cloud_batch_size = cloud_batch_size

        # HTTP 重试策略参数（延迟创建实际策略对象）
        self._retry_total = retry_total
        self._retry_backoff_factor = retry_backoff_factor
        self._retry_status_forcelist = retry_status_forcelist or [429, 500, 502, 503, 504]

        # Session 复用（延迟创建）
        self._session = None

        # 维度缓存
        self._dimension_cache: Optional[int] = None

        # 模型状态
        self._model_state = self._STATE_NOT_LOADED

        if self.use_cloud_embedding:
            # 云端模式
            self._cloud_config = cloud_config
            self._cloud_api_key = cloud_config.api_key
            self._cloud_model = cloud_config.model
            self._cloud_dimension = cloud_config.dimension
            self._cloud_api_type = cloud_config.api_type
            self._cloud_api_full_url = cloud_config.api_full_url
            self._cloud_supports_batch = cloud_config.supports_batch
            self.embedding_model_name = None
            self._local_model_path = None
            self._embedding_model = None
        else:
            # 本地模式
            self._local_config = local_config
            self.embedding_model_name = local_config.model_name
            self._local_model_path = local_config.model_path
            self._embedding_model = self._init_local_model()
            self._model_state = self._STATE_LOADED
            self._cloud_config = None
            self._cloud_api_key = None
            self._cloud_model = None
            self._cloud_dimension = None
            self._cloud_api_type = None
            self._cloud_api_full_url = None
            self._cloud_supports_batch = False

    def _get_session(self):
        """获取或创建复用的 HTTP Session（带重试策略）。"""
        if self._session is None:
            _ensure_requests()
            retry_strategy = _Retry(
                total=self._retry_total,
                backoff_factor=self._retry_backoff_factor,
                status_forcelist=self._retry_status_forcelist,
                allowed_methods=["POST"],
            )
            self._session = _requests.Session()
            adapter = _HTTPAdapter(max_retries=retry_strategy)
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)
        return self._session

    def _close_session(self) -> None:
        """关闭 HTTP Session。"""
        if self._session is not None:
            self._session.close()
            self._session = None

    @classmethod
    def create_cloud(
        cls,
        api_key: str,
        model: str,
        api_type: str,
        api_full_url: str,
        dimension: Optional[int] = None,
        verbose: bool = True,
        **kwargs: Any,
    ) -> 'EmbeddingEncoder':
        """
        创建云端向量编码器（工厂方法）

        Args:
            api_key: 云端 API Key
            model: 云端模型名称
            api_type: 云端 API 类型，"text" 或 "multimodal"
            api_full_url: 云端 API 完整 URL
            dimension: 云端向量维度（可选）
            verbose: 是否输出详细信息
            **kwargs: 传递给 EmbeddingEncoder 的额外参数（retry_total 等）

        Returns:
            EmbeddingEncoder 实例
        """
        cloud_config = CloudEmbeddingConfig(
            api_key=api_key,
            model=model,
            api_type=api_type,
            api_full_url=api_full_url,
            dimension=dimension
        )
        return cls(cloud_config=cloud_config, verbose=verbose, **kwargs)

    @classmethod
    def create_local(
        cls,
        model_name: str,
        model_path: Path,
        verbose: bool = True,
        **kwargs: Any,
    ) -> 'EmbeddingEncoder':
        """
        创建本地向量编码器（工厂方法）

        Args:
            model_name: 本地模型名称
            model_path: 本地模型路径
            verbose: 是否输出详细信息
            **kwargs: 传递给 EmbeddingEncoder 的额外参数

        Returns:
            EmbeddingEncoder 实例
        """
        local_config = LocalEmbeddingConfig(
            model_name=model_name,
            model_path=model_path
        )
        return cls(local_config=local_config, verbose=verbose, **kwargs)

    def _init_local_model(self) -> Any:
        """
        检查模型目录完整性（配置文件 + 模型文件），并加载 SentenceTransformer 模型

        Returns:
            加载好的 SentenceTransformer 模型实例

        Raises:
            FileNotFoundError: 模型目录缺少必要文件
            EmbeddingModelLoadError: 模型加载失败
        """
        # 检查配置文件
        config_files = ["config.json", "config_sentence_transformers.json"]
        has_config = any((self._local_model_path / cfg).exists() for cfg in config_files)
        if not has_config:
            raise FileNotFoundError(
                f"模型配置文件不存在: {self._local_model_path}\n"
                f"请检查模型目录是否完整"
            )

        # 检查模型文件
        model_files = ["pytorch_model.bin", "model.safetensors"]
        has_model = any((self._local_model_path / mf).exists() for mf in model_files)
        if not has_model:
            raise FileNotFoundError(
                f"模型文件不存在: {self._local_model_path}\n"
                f"请检查模型文件是否完整"
            )

        # 加载模型
        abs_path = str(self._local_model_path.resolve())
        logger.info("加载模型: %s", abs_path)
        logger.info("模型名称: %s", self.embedding_model_name)

        try:
            # 延迟导入：避免在仅使用云端 embedding 时引入 torch / sentence_transformers 依赖
            from sentence_transformers import SentenceTransformer  # type: ignore[import]
            model = SentenceTransformer(abs_path, trust_remote_code=True)
            logger.info("模型加载成功")
            return model
        except FileNotFoundError:
            raise
        except Exception as e:
            logger.error("模型加载失败: %s", e)
            raise EmbeddingModelLoadError(
                f"无法加载模型 '{self.embedding_model_name}' 从路径 '{abs_path}': {e}"
            ) from e

    def encode_texts(self, texts: List[str]) -> np.ndarray:
        """
        批量向量化：将多条文本一次性转换为向量，适用于文档入库等批量场景

        Args:
            texts: 文本列表，如 ["文档片段1", "文档片段2", ...]

        Returns:
            二维向量数组，shape=(n_texts, dimension)，每行对应一条文本的向量

        Raises:
            ValueError: 当文本列表为空时
        """
        if not texts:
            raise ValueError("输入的准备向量化的内容不能为空")

        if self.use_cloud_embedding:
            return self._encode_with_cloud(texts)
        else:
            return self._encode_with_local(texts)

    def encode_query(self, query: str) -> np.ndarray:
        """
        单条向量化：将一条查询文本转换为向量，适用于用户检索场景

        Args:
            query: 单条查询文本，如 "什么是向量数据库"

        Returns:
            一维向量，shape=(dimension,)
        """
        embeddings = self.encode_texts([query])
        return embeddings[0]

    def _encode_with_local(self, texts: List[str]) -> np.ndarray:
        """
        使用本地 SentenceTransformer 模型进行向量化，数据量超过 batch_size 时自动分批处理

        Args:
            texts: 待向量化的文本列表

        Returns:
            二维向量数组，shape=(n_texts, dimension)

        Raises:
            RAGComponentError: 模型未初始化或已卸载
        """
        if self._embedding_model is None:
            if self._model_state == self._STATE_UNLOADED:
                raise RAGComponentError(
                    "本地模型已通过 unload_model() 卸载，"
                    "如需继续使用请重新创建 EmbeddingEncoder 实例"
                )
            raise RAGComponentError("本地模型未初始化")

        total_texts = len(texts)
        batch_size = self._local_batch_size

        if total_texts <= batch_size:
            if self.verbose:
                logger.info("向量化 %d 个文本块", total_texts)
            return self._embedding_model.encode(texts, show_progress_bar=_SHOW_PROGRESS_BAR)

        # 批量处理
        total_batches = (total_texts + batch_size - 1) // batch_size
        if self.verbose:
            logger.info("向量化 %d 个文本块，分 %d 个批次", total_texts, total_batches)

        all_embeddings = []
        for i in range(0, total_texts, batch_size):
            batch_texts = texts[i:i + batch_size]
            batch_num = i // batch_size + 1

            if self.verbose:
                logger.info("批次 %d/%d (%d 个文本块)...", batch_num, total_batches, len(batch_texts))

            batch_embeddings = self._embedding_model.encode(
                batch_texts,
                show_progress_bar=_SHOW_PROGRESS_BAR,
                batch_size=batch_size,
                convert_to_numpy=True
            )
            all_embeddings.append(batch_embeddings)

            if self.verbose:
                logger.info("批次 %d/%d 完成", batch_num, total_batches)

        embeddings_array = np.vstack(all_embeddings)

        if self.verbose:
            logger.info(
                "向量化完成，共 %d 个向量（维度: %d）",
                embeddings_array.shape[0], embeddings_array.shape[1],
            )

        return embeddings_array

    def _build_payload(
        self,
        input_data: Union[str, List[str], List[Dict[str, str]]]
    ) -> Dict[str, Any]:
        """
        构建云端 Embedding API 的请求体，包含模型名称、输入数据、编码格式，以及可选的维度参数

        Args:
            input_data: 文本字符串（单条）、文本列表（批量）或多模态格式数据

        Returns:
            可直接用于 requests.post(json=payload) 的字典
        """
        payload: Dict[str, Any] = {
            "model": self._cloud_model,
            "input": input_data,
            "encoding_format": "float"
        }

        if self._cloud_dimension is not None and self._cloud_dimension in self._supported_dimensions:
            payload["dimensions"] = self._cloud_dimension

        return payload

    def _encode_with_cloud(self, texts: List[str]) -> np.ndarray:
        """
        使用云端 API 进行向量化，自动处理文本截断、批量/逐条调用、响应解析

        Args:
            texts: 待向量化的文本列表

        Returns:
            二维向量数组，shape=(n_texts, dimension)
        """
        # 延迟导入，避免循环导入
        from ..utils import estimate_tokens

        url = self._cloud_api_full_url
        use_text_api = self._cloud_api_type == "text"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._cloud_api_key}"
        }

        embeddings = []
        max_tokens_per_text = self._cloud_max_tokens_per_text
        batch_size = self._cloud_batch_size if self._cloud_supports_batch else 1

        total_batches = (len(texts) + batch_size - 1) // batch_size
        if self.verbose:
            api_type_str = "文本向量化 API" if use_text_api else "多模态向量化 API"
            logger.info("使用 %s", api_type_str)
            logger.info("开始向量化 %d 个文本块，分 %d 个批次处理", len(texts), total_batches)

        session = self._get_session()

        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i:i + batch_size]
            batch_num = i // batch_size + 1

            # 截断过长的文本
            filtered_batch = []
            for text in batch_texts:
                estimated_tokens = estimate_tokens(text)
                if estimated_tokens > max_tokens_per_text:
                    safe_length = int(max_tokens_per_text / _TOKENS_TO_CHARS_RATIO)
                    text = text[:safe_length]
                filtered_batch.append(text)

            # 如果不支持批量，逐个调用
            if not self._cloud_supports_batch:
                for text in filtered_batch:
                    embedding = self._call_single_api(text, url, headers, use_text_api, session)
                    embeddings.append(embedding)
                continue

            # 批量调用
            try:
                if use_text_api:
                    input_data = filtered_batch
                else:
                    input_data = [{"type": "text", "text": text} for text in filtered_batch]

                payload = self._build_payload(input_data)

                response = session.post(url, headers=headers, json=payload, timeout=30)
                response.raise_for_status()
                result = response.json()

                # 提取向量
                batch_embeddings = []
                if 'data' in result:
                    data = result['data']
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict) and 'embedding' in item:
                                batch_embeddings.append(item['embedding'])
                    elif isinstance(data, dict):
                        if 'embedding' in data:
                            batch_embeddings.append(data['embedding'])
                        elif 'embeddings' in data:
                            batch_embeddings.extend(data['embeddings'])

                if not batch_embeddings:
                    logger.warning(
                        "批次 %d/%d 返回空向量结果，请检查 API 响应格式",
                        batch_num, total_batches,
                    )
                embeddings.extend(batch_embeddings)

                if self.verbose:
                    logger.info("批次 %d/%d 完成", batch_num, total_batches)

            except Exception as e:
                logger.error("向量化批次 %d 失败: %s", batch_num, e)
                raise

        if len(embeddings) == 0:
            raise ValueError("未能提取到任何向量")

        return np.array(embeddings)

    def _call_single_api(
        self,
        text: str,
        url: str,
        headers: Dict[str, str],
        use_text_api: bool,
        session: Any = None,
    ) -> List[float]:
        """
        调用云端 API 对单条文本进行向量化（用于不支持批量的 multimodal 类型）

        Args:
            text: 单条文本
            url: API 请求地址
            headers: 请求头（含 Authorization）
            use_text_api: True 时直接传文本字符串，False 时包装为多模态格式
            session: 复用的 HTTP Session

        Returns:
            单条文本的向量，List[float]
        """
        if use_text_api:
            input_data = text
        else:
            input_data = [{"type": "text", "text": text}]

        payload = self._build_payload(input_data)

        if session is None:
            session = self._get_session()
        response = session.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()

        if 'data' in result:
            data = result['data']
            if isinstance(data, dict) and 'embedding' in data:
                return data['embedding']
            elif isinstance(data, list) and len(data) > 0:
                if isinstance(data[0], dict) and 'embedding' in data[0]:
                    return data[0]['embedding']

        raise ValueError("单个向量 API 响应格式错误")

    def get_embedding_config(self) -> Dict[str, Any]:
        """
        获取当前向量化配置摘要（云端返回 API 信息，本地会实际编码一次以获取模型维度）

        Returns:
            包含 use_cloud_embedding、model、dimension 等字段的字典
        """
        if self.use_cloud_embedding:
            # 脱敏处理：仅返回 URL 的 host 部分，避免泄露完整内部 API 地址
            from urllib.parse import urlparse
            masked_url = None
            if self._cloud_api_full_url:
                parsed = urlparse(self._cloud_api_full_url)
                masked_url = f"{parsed.scheme}://{parsed.hostname}/***"
            return {
                "use_cloud_embedding": True,
                "api_type": self._cloud_api_type,
                "api_full_url": masked_url,
                "model": self._cloud_model,
                "dimension": self._cloud_dimension,
                "supports_batch": self._cloud_supports_batch
            }
        else:
            # 本地模型：尝试获取实际维度（带缓存）
            dimension = self._dimension_cache
            if dimension is None:
                try:
                    if self._embedding_model is not None:
                        test_embedding = self._embedding_model.encode([_DIMENSION_TEST_TEXT])
                        dimension = (
                            test_embedding.shape[1]
                            if len(test_embedding.shape) > 1
                            else len(test_embedding[0])
                        )
                        self._dimension_cache = dimension
                except Exception:
                    pass

            return {
                "use_cloud_embedding": False,
                "api_type": None,
                "api_full_url": None,
                "model": self.embedding_model_name,
                "dimension": dimension,
                "supports_batch": False
            }

    def unload_model(self) -> None:
        """
        卸载模型，释放内存

        用于显式释放本地模型占用的GPU/CPU内存，适用于：
        - 模型不再需要时的资源清理
        - 内存不足时的释放操作
        - 程序退出前的资源释放

        注意：只对本地模型有效，云端模式调用此方法无影响。
        卸载后再调用 encode_texts 会抛出 RAGComponentError，需重新创建实例。
        """
        if not self.use_cloud_embedding and self._embedding_model is not None:
            if self.verbose:
                logger.info("正在卸载本地向量化模型...")
            del self._embedding_model
            self._embedding_model = None
            self._model_state = self._STATE_UNLOADED
            self._dimension_cache = None

            # 触发垃圾回收以立即释放内存
            gc.collect()

            if self.verbose:
                logger.info("模型已卸载，内存已释放")

        # 同时关闭 HTTP Session
        self._close_session()

    def __enter__(self) -> 'EmbeddingEncoder':
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """上下文管理器退出，自动清理资源"""
        self.unload_model()
