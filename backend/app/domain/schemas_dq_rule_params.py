"""DQ 规则结构化参数 Pydantic 模型（feat-dq-rule-params v1 隔离命名空间）。

8 种 kind 通过 Literal 联合 + Discriminator 路由。校验失败抛 ValidationError，
错误路径精确到字段，FastAPI 自动转 422。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from re import compile as re_compile
from typing import Annotated, Any, Literal, Union

from app.domain.enums import RuleType, Severity
from pydantic import (
    BaseModel, ConfigDict, Discriminator, Field, TypeAdapter,
    field_validator, model_validator,
)
from pydantic.alias_generators import to_camel


class _Base(BaseModel):
    """DTO 基类：snake_case 字段名 + camelCase JSON 别名，与项目其他 DTO 对齐。"""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

_IDENT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_ALLOWED_OPS = (">", ">=", "<", "<=", "=", "!=")


class _NotNullParams(_Base):
    kind: Literal["not_null"]


class _UniqueParams(_Base):
    kind: Literal["unique"]


class _RangeParams(_Base):
    kind: Literal["range"]
    min: Decimal | None = None
    max: Decimal | None = None

    @model_validator(mode="after")
    def _atLeastOne(self) -> "_RangeParams":
        if self.min is None and self.max is None:
            raise ValueError("range 必须至少给 min 或 max 之一")
        return self


class _InSetParams(_Base):
    kind: Literal["in_set"]
    values: list[str] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def _validateValues(cls, v: list[str]) -> list[str]:
        for item in v:
            if len(item) < 1 or len(item) > 50:
                raise ValueError(f"值 {item!r} 长度需 1-50 字符")
            for ch in item:
                if ch in ("'", "\\") or ord(ch) < 0x20:
                    raise ValueError(f"值 {item!r} 含非法字符")
        return v


class _RegexParams(_Base):
    kind: Literal["regex"]
    pattern: str = Field(min_length=1, max_length=500)

    @field_validator("pattern")
    @classmethod
    def _validatePattern(cls, v: str) -> str:
        for ch in v:
            if ch in ("'", "\\") or ord(ch) < 0x20:
                raise ValueError(f"正则表达式含非法字符: {v!r}")
        try:
            re_compile(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"正则表达式非法: {exc}") from exc
        return v


class _CompareParams(_Base):
    kind: Literal["compare"]
    op: Literal[">", ">=", "<", "<=", "=", "!="]
    value: Decimal


class _RefParams(_Base):
    kind: Literal["ref"]
    ref_table: str = Field(pattern=_IDENT_PATTERN)
    ref_column: str = Field(pattern=_IDENT_PATTERN)


class _CrossColumnParams(_Base):
    kind: Literal["cross_column"]
    left: str = Field(pattern=_IDENT_PATTERN)
    op: Literal[">", ">=", "<", "<=", "=", "!="]
    right: str = Field(pattern=_IDENT_PATTERN)
    factor: Decimal | None = None

    @model_validator(mode="after")
    def _factorNonZero(self) -> "_CrossColumnParams":
        if self.factor is not None and self.factor == 0:
            raise ValueError("factor 不能为 0")
        return self


RuleParamsUnion = Annotated[
    Union[
        _NotNullParams, _UniqueParams, _RangeParams, _InSetParams,
        _RegexParams, _CompareParams, _RefParams, _CrossColumnParams,
    ],
    Discriminator("kind"),
]

_adapter = TypeAdapter(RuleParamsUnion)


class RuleParams:
    """判别联合入口：model_validate 按 kind 路由，返回具体 kind 模型实例。

    Annotated 联合本身不是 BaseModel，没有 model_validate，故以 facade 类
    承载；作为 Pydantic 字段注解时请用 RuleParamsUnion。
    """

    @classmethod
    def model_validate(cls, obj: Any) -> Any:
        return _adapter.validate_python(obj)


class RuleParamsRead(_Base):
    """响应里回显的 params 形态：kind + 原始 payload（前端回填用）。"""
    kind: str
    raw: dict


class DataQualityRuleParamsCreate(_Base):
    """结构化模式 create DTO；service 写入时编译 params → rule_expression。"""
    rule_code: str = Field(min_length=1, max_length=200)
    rule_name: str = Field(min_length=1, max_length=200)
    rule_type: RuleType
    target_table: str = Field(pattern=_IDENT_PATTERN)
    target_column: str | None = Field(default=None, pattern=_IDENT_PATTERN)
    threshold: Decimal
    severity: Severity
    datasource_id: int
    rule_params: dict | None = None
    rule_expression: str | None = None

    @model_validator(mode="after")
    def _mutex(self) -> "DataQualityRuleParamsCreate":
        params = self.rule_params
        expr = self.rule_expression
        if self.rule_type in (RuleType.VALIDITY, RuleType.CONSISTENCY, RuleType.REFERENTIAL):
            if params is None and expr is None:
                raise ValueError(
                    f"{self.rule_type.value} 规则必须提供 rule_params 或 rule_expression"
                )
        if params is not None and expr is not None:
            raise ValueError("结构化模式忽略客户端 rule_expression；不要两者同时提供")
        return self


class DataQualityRuleParamsUpdate(_Base):
    """结构化模式 update DTO；互斥规则同 create。"""
    rule_name: str | None = Field(default=None, min_length=1, max_length=200)
    threshold: Decimal | None = None
    severity: Severity | None = None
    target_column: str | None = Field(default=None, pattern=_IDENT_PATTERN)
    rule_params: dict | None = None
    rule_expression: str | None = None

    @model_validator(mode="after")
    def _mutex(self) -> "DataQualityRuleParamsUpdate":
        if self.rule_params is not None and self.rule_expression is not None:
            raise ValueError("结构化模式忽略客户端 rule_expression；不要两者同时提供")
        return self


class DataQualityRuleParamsRead(_Base):
    """结构化模式 read DTO；config_mode 由 rule_params 是否存在派生。"""
    id: int
    rule_code: str
    rule_name: str
    rule_type: RuleType
    target_table: str
    target_column: str | None
    threshold: Decimal
    severity: Severity
    datasource_id: int
    rule_expression: str | None
    rule_params: dict | None
    config_mode: Literal["structured", "custom"]
    created_time: datetime | None = None
    updated_time: datetime | None = None
