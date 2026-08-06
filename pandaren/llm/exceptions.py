"""pandaren/llm/exceptions.py — LLM 异常层次

所有 LLM 调用失败均抛出此文件中定义的强类型异常，
httpx 原始异常不允许逃逸到 AgentLoop 层。

异常层次：
  LLMError
  ├── LLMAuthError          HTTP 401/403，不重试
  ├── LLMRequestError       HTTP 400，不重试
  ├── LLMRateLimitError     HTTP 429，可重试（携带 retry_after）
  ├── LLMServerError        HTTP 5xx，可重试
  ├── LLMNetworkError       连接/DNS/SSL 失败，可重试
  │   └── LLMTimeoutError   超时（httpx.TimeoutException / HTTP 408），可重试
  └── LLMResponseError      响应 JSON 解析失败，不重试
"""

from __future__ import annotations


class LLMError(Exception):
    """所有 LLM 异常的基类。"""


class LLMAuthError(LLMError):
    """HTTP 401 / 403：认证失败或权限不足，不重试。"""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMRequestError(LLMError):
    """HTTP 400：请求内容非法（context_length_exceeded / invalid_request），不重试。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMRateLimitError(LLMError):
    """HTTP 429：速率限制，可重试。

    retry_after：从 Retry-After header 读取的等待秒数；
                 若 header 不存在则为 None（由 Loop 决定等待时长）。
    duration_ms：从发起请求到异常抛出的耗时，供 engine 层传给 on_after_llm_call。
    """

    def __init__(self, message: str, retry_after: float | None = None, *, duration_ms: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.duration_ms = duration_ms


class LLMServerError(LLMError):
    """HTTP 5xx（及其他非 2xx）：服务端错误，可重试。"""

    def __init__(self, message: str, status_code: int, *, duration_ms: float = 0.0) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.duration_ms = duration_ms


class LLMNetworkError(LLMError):
    """连接失败 / DNS 解析失败 / SSL 错误，可重试。"""

    def __init__(self, message: str = "", *, duration_ms: float = 0.0) -> None:
        super().__init__(message)
        self.duration_ms = duration_ms


class LLMTimeoutError(LLMNetworkError):
    """连接或读取超时（httpx.TimeoutException / HTTP 408），可重试。

    是 LLMNetworkError 的子类，上层可统一捕获 LLMNetworkError，
    也可单独捕获 LLMTimeoutError 进行更短的重试间隔处理。
    """


class LLMResponseError(LLMError):
    """响应格式解析失败（如 provider 返回非 JSON），不重试。"""
