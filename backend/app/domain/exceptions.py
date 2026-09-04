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


class ConflictError(DomainError):
    """资源冲突（唯一约束 / 业务规则不允许重复等）。"""


class ValidationError(DomainError):
    """输入校验失败（领域规则层面）。

    Phase 6.5 扩展：details 携带结构化数据（如供应商名歧义候选列表），
    由全局 handler 透传到 422 响应体。与 detail（str，折叠展示用）互不影响。
    """

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.details = details


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


class PermissionDeniedError(DomainError):
    """权限不足（owner 不匹配 + 非 admin 角色）。

    Phase 4.5 governance hardening：当前仅用于 KpiCatalogService 写入路径，
    后续可扩展到其他实体的 PUT/DELETE 检查。
    """


class BusinessObjectGraphLabelMismatchError(ValidationError):
    """graph_label 与 header_class.class_name 不一致 (Phase 4.4)。

    基类取 ValidationError（写时业务规则校验 → 422）；本代码库无 BusinessRuleError。
    """

    def __init__(self, code: str, graph_label: str, class_name: str) -> None:
        super().__init__(
            f"业务对象 {code} 的 graph_label={graph_label!r} 与 "
            f"本体类 class_name={class_name!r} 不一致"
        )
        self.code = code
        self.graph_label = graph_label
        self.class_name = class_name
