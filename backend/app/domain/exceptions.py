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


# ---------------------------------------------------------------------------
# feat-feature-rule-config (Phase 9): Feature Rule DTO + exceptions
# ---------------------------------------------------------------------------


class FeatureRuleNotFoundError(NotFoundError):
    """Feature rule 不存在（spec §9.2）。"""


class FeatureRuleVersionConflictError(ConflictError):
    """Feature rule 乐观锁版本冲突（spec §9.2）。"""

    def __init__(self, message: str, *, current_version: int) -> None:
        super().__init__(message)
        self.current_version = current_version
        self.details = {"current_version": current_version}


class FeatureRuleReferencingError(ConflictError):
    """Feature rule 被外部引用，无法删除（spec §9.2）。"""

    def __init__(self, message: str, *, referencing: list[str]) -> None:
        super().__init__(message)
        self.referencing = referencing
        self.details = {"referencing": referencing}


class FeatureRuleValidationError(ValidationError):
    """Feature rule 业务校验失败（spec §9.2）。"""


class LLMUnavailableError(DomainError):
    """LLM 服务不可用（spec §9.2 + §7.3）。"""


# ---------------------------------------------------------------------------
# feat-wiki-dedup (P1): 内容重复
# ---------------------------------------------------------------------------


class DuplicatePageError(ConflictError):
    """同 ``page_id`` 且 ``content_hash`` 相同 —— 确证是同一份知识的重跑。

    刻意继承 ``ConflictError``：如果它意外逃到 API 层，``statusForError`` 仍给出
    409（语义正确）。但**导入路径必须单独 catch 它**（catch 在 ``ConflictError``
    之前），否则它会被当成失败计数 —— 那正是 P1 要修的分类学错误。
    """

    def __init__(self, message: str, *, page_id: str) -> None:
        super().__init__(message)
        self.page_id = page_id


def statusForError(exc: DomainError) -> int:
    """领域异常 → HTTP 状态码（**唯一**映射源）。

    生产 app（``main.py``）与测试 app（``tests/_testapp.py``）共用此函数。
    此前两处各写一份，``LLMUnavailableError`` 只在生产那份里有 → 测试断言
    503 而测试 app 实际返回 400，断言与真实行为脱节。
    """
    if isinstance(exc, NotFoundError):
        return 404
    if isinstance(exc, ConflictError):
        return 409
    if isinstance(exc, ValidationError):
        # 一并覆盖子类 BusinessObjectGraphLabelMismatchError /
        # FeatureRuleValidationError 等
        return 422
    if isinstance(exc, PermissionDeniedError):
        return 403
    if isinstance(exc, LLMUnavailableError):
        return 503
    return 400
