"""领域异常层次结构。

每个异常携带用户友好的 message 与可选 detail。服务层捕获底层异常并转换为领域异常。
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """所有领域异常的基类。"""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ConfigError(DomainError):
    """配置缺失或非法。"""


class NotFoundError(DomainError):
    """资源不存在。"""


class ValidationError(DomainError):
    """输入校验失败（领域规则层面）。"""


class LlmClientError(DomainError):
    """LLM 调用失败。"""

    def __init__(self, message: str, *, provider: str | None = None, detail: str | None = None) -> None:
        super().__init__(message, detail=detail)
        self.provider = provider


class NoAvailableModelError(DomainError):
    """无可用的模型（全部被熔断或停用）。"""


class SqlSafetyError(DomainError):
    """SQL 未通过安全校验。"""

    def __init__(self, message: str, *, sql: str | None = None, detail: str | None = None) -> None:
        super().__init__(message, detail=detail)
        self.sql = sql


class DataSourceError(DomainError):
    """数据源连接或执行失败。"""


class OntologyError(DomainError):
    """本体操作失败。"""


class ChartRenderError(DomainError):
    """图表渲染失败。"""


class Nl2SqlError(DomainError):
    """自然语言转 SQL 失败（重试耗尽或无有效 SQL）。

    tokens 携带各次尝试累计消耗的 (promptTokens, completionTokens)，
    供模型降级审计行如实计量已消耗的 token。
    """

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        tokens: tuple[int, int] | None = None,
        lastPlan: Any | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.tokens = tokens
        self.lastPlan = lastPlan


class MilvusError(DomainError):
    """Milvus 向量检索/存储失败。"""
