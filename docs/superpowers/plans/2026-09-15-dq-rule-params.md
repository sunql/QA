# DQ Rule Params Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为数据质量规则添加结构化参数配置（v1：手工表单）。结构化模式 `rule_params` 为 SSOT，写入时编译为 `rule_expression`，评估器零改动。DB 共用，前后端独立交付。

**Architecture:**
- DB: `data_quality_rule` 加 `rule_params JSONB NULL`（Alembic 0076，零回填）
- 后端：新增 4 个文件（compiler / schemas / service / router），`main.py` 注册一行
- 前端：新增 4 个文件（form / page / summary util / api client）+ 菜单注册 + 路由
- 编译时机 = 写入时；评估路径 = 既有 `rule_expression` 读取（字节级回归）

**Tech Stack:** FastAPI + SQLAlchemy 2.x async + Alembic + Pydantic v2 / React + antd + Vite + Vitest + i18next

## Global Constraints

- 隔离交付：除 `main.py` 加一行 router、`App.tsx` 加路由、`seed_menu_config.py` 加菜单、i18n 加 keys 外，**不修改任何现有文件**
- 评估器零改动：5 个 evaluator、`_common.py`、`data_quality_evaluator.py` 一律不动；编译时机 = 写入时保证 evaluator 永远只读 `rule_expression`
- 测试规范：后端集成测试必须真实 PG（按 `Harness/rules/测试规范.md`），TDD 红→绿→重构
- 函数 < 50 行、文件 < 800 行、嵌套 ≤ 4 层
- 覆盖率 ≥ 80%（项目硬约束）
- Python 命名偏离 PEP 8：`camelCase`（与前端规则一致，DB 列/JSON 契约保持 `snake_case`）
- DB 操作前先确认测试库（参考 `Harness/rules/数据库环境使用规范.md`）
- 每个有意义步骤完成后 `git commit`；commit 信息遵循 conventional commits
- 代码完成立即 `code-reviewer` / `security-reviewer` 审查

## File Structure（全部新增文件）

**Backend:**
- `backend/alembic/versions/0076_data_quality_rule_params.py` — 迁移
- `backend/app/domain/schemas_dq_rule_params.py` — Pydantic 模型（独立命名空间）
- `backend/app/services/data_quality_evaluators/rule_params_compiler.py` — 纯函数编译器
- `backend/app/services/data_quality_rule_params_service.py` — CRUD + 编译编排
- `backend/app/api/v1/data_quality_rule_params.py` — 新 router
- `backend/app/tests/unit/test_rule_params_compiler.py`
- `backend/app/tests/unit/test_data_quality_rule_params_service.py`
- `backend/app/tests/integration/test_data_quality_rule_params_api.py`

**Backend（仅追加，不破坏）：**
- `backend/app/main.py` — +2 行（import + include_router）

**Frontend:**
- `frontend/src/api/dataQualityRuleParams.ts`
- `frontend/src/utils/ruleParamsSummary.ts`
- `frontend/src/components/dq/RuleParamsForm.tsx`
- `frontend/src/pages/DataQualityRuleParamsPage.tsx`
- `frontend/src/tests/ruleParamsSummary.test.ts`
- `frontend/src/tests/RuleParamsForm.test.tsx`
- `frontend/src/tests/DataQualityRuleParamsPage.test.tsx`

**Frontend（仅追加）：**
- `frontend/src/App.tsx` — +1 路由
- `backend/scripts/seed_menu_config.py` — +1 菜单项
- `frontend/src/i18n/zh-CN.ts` 与 `frontend/src/i18n/en-US.ts` — +1 命名空间

---

## Task 1: Alembic 迁移 — 加 `rule_params` JSONB 列

**Files:**
- Create: `backend/alembic/versions/0076_data_quality_rule_params.py`
- Test: `backend/alembic/tests/test_0076_migration.py`（如项目有该目录，否则用 `backend/app/tests/integration/test_alembic_0076.py`）

**Interfaces:**
- Consumes: SQLAlchemy `op` API
- Produces: `data_quality_rule.rule_params JSONB NULL` 列；upgrade / downgrade 双向幂等

- [ ] **Step 1: 写失败测试**

参考 `backend/alembic/versions/0060_*.py` 现有迁移风格。测试在隔离测试库跑：

```python
# backend/app/tests/integration/test_alembic_0076.py
import pytest
from alembic.config import Config
from alembic import command
from sqlalchemy import inspect, text

@pytest.mark.integration
async def test_0076_adds_rule_params_column(pg_engine, alembic_cfg):
    command.upgrade(alembic_cfg, "head")
    async with pg_engine.begin() as conn:
        cols = await conn.run_sync(lambda c: inspect(c).get_columns("data_quality_rule"))
    col_names = [c["name"] for c in cols]
    assert "rule_params" in col_names
    rule_params_col = next(c for c in cols if c["name"] == "rule_params")
    assert rule_params_col["nullable"] is True
    assert "json" in str(rule_params_col["type"]).lower()
```

`pg_engine` / `alembic_cfg` fixtures 复用 `backend/app/tests/integration/test_data_quality_api.py` 顶部既有定义。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && pytest app/tests/integration/test_alembic_0076.py -v
```

Expected: FAIL（迁移文件不存在）。

- [ ] **Step 3: 写最小迁移**

```python
# backend/alembic/versions/0076_data_quality_rule_params.py
"""add rule_params JSONB column to data_quality_rule

feat-dq-rule-params v1: 结构化规则参数存储列。字段可空，存量规则零迁移。
"""
from alembic import op
import sqlalchemy as sa

revision = "0076_data_quality_rule_params"
down_revision = "0075_dq_sample_error"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_quality_rule",
        sa.Column("rule_params", sa.JSON(none_as_null=True), nullable=True),
    )
    op.execute(
        "COMMENT ON COLUMN data_quality_rule.rule_params IS "
        "'结构化规则参数；NULL=自定义SQL模式(legacy)，非NULL=结构化模式'"
    )


def downgrade() -> None:
    op.drop_column("data_quality_rule", "rule_params")
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && pytest app/tests/integration/test_alembic_0076.py -v
```

Expected: PASS。

- [ ] **Step 5: 跑既有集成测试套确认无回归**

```bash
cd backend && pytest app/tests/integration/test_data_quality_api.py -v
```

Expected: PASS（迁移新增列 = 可空，对既有 INSERT 不影响）。

- [ ] **Step 6: 提交**

```bash
git add backend/alembic/versions/0076_data_quality_rule_params.py \
        backend/app/tests/integration/test_alembic_0076.py
git commit -m "feat(dq): add rule_params JSONB column (alembic 0076)"
```

---

## Task 2: Pydantic Schemas — 8 种 kind 的判别联合

**Files:**
- Create: `backend/app/domain/schemas_dq_rule_params.py`
- Test: `backend/app/tests/unit/test_schemas_dq_rule_params.py`

**Interfaces:**
- Consumes: `RuleType` enum（`app.domain.enums`）
- Produces: 8 个 kind 模型 + `RuleParams` discriminated union + `RuleParamsRead` + DTO

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/unit/test_schemas_dq_rule_params.py
import pytest
from pydantic import ValidationError
from app.domain.schemas_dq_rule_params import (
    RuleParams, _RangeParams, _InSetParams, _CompareParams,
    _RefParams, _CrossColumnParams, _RegexParams, _NotNullParams, _UniqueParams,
)


class TestNotNull:
    def test_accepts_empty(self):
        p = RuleParams.model_validate({"kind": "not_null"})
        assert isinstance(p, _NotNullParams)


class TestUnique:
    def test_accepts_empty(self):
        p = RuleParams.model_validate({"kind": "unique"})
        assert isinstance(p, _UniqueParams)


class TestRange:
    def test_min_only(self):
        p = RuleParams.model_validate({"kind": "range", "min": 0})
        assert p.min == 0 and p.max is None

    def test_max_only(self):
        p = RuleParams.model_validate({"kind": "range", "max": 100})

    def test_both(self):
        p = RuleParams.model_validate({"kind": "range", "min": 0, "max": 100})

    def test_neither_raises(self):
        with pytest.raises(ValidationError) as exc:
            RuleParams.model_validate({"kind": "range"})
        assert "min" in str(exc.value) or "max" in str(exc.value)

    def test_non_decimal_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "range", "min": "abc"})


class TestInSet:
    def test_values(self):
        p = RuleParams.model_validate({"kind": "in_set", "values": ["A", "B"]})
        assert p.values == ["A", "B"]

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "in_set", "values": []})

    def test_quote_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "in_set", "values": ["A'B"]})

    def test_control_char_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "in_set", "values": ["A\x00B"]})

    def test_too_long_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "in_set", "values": ["x" * 51]})


class TestRegex:
    def test_pattern(self):
        p = RuleParams.model_validate({"kind": "regex", "pattern": "^[0-9]+$"})
        assert p.pattern == "^[0-9]+$"

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "regex", "pattern": ""})

    def test_too_long_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "regex", "pattern": "x" * 501})

    def test_invalid_regex_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "regex", "pattern": "[unclosed"})


class TestCompare:
    def test_gt(self):
        p = RuleParams.model_validate({"kind": "compare", "op": ">", "value": 0})
        assert p.op == ">"

    def test_invalid_op_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "compare", "op": "~~", "value": 0})


class TestRef:
    def test_valid(self):
        p = RuleParams.model_validate({
            "kind": "ref", "ref_table": "SUPPLIER", "ref_column": "SUPPLIER_KEY",
        })

    def test_invalid_identifier_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({
                "kind": "ref", "ref_table": "1SUPPLIER", "ref_column": "X",
            })


class TestCrossColumn:
    def test_basic(self):
        p = RuleParams.model_validate({
            "kind": "cross_column", "left": "RECEIVED_QTY", "op": "<=",
            "right": "ORDER_QTY", "factor": 1.05,
        })

    def test_factor_default(self):
        p = RuleParams.model_validate({
            "kind": "cross_column", "left": "A", "op": "=", "right": "B",
        })
        assert p.factor is None

    def test_factor_zero_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({
                "kind": "cross_column", "left": "A", "op": "=", "right": "B", "factor": 0,
            })


class TestDiscriminator:
    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            RuleParams.model_validate({"kind": "unknown"})
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && pytest app/tests/unit/test_schemas_dq_rule_params.py -v
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 写实现**

```python
# backend/app/domain/schemas_dq_rule_params.py
"""DQ 规则结构化参数 Pydantic 模型（feat-dq-rule-params v1 隔离命名空间）。

8 种 kind 通过 Literal 联合 + Discriminator 路由。校验失败抛 ValidationError，
错误路径精确到字段，FastAPI 自动转 422。
"""
from __future__ import annotations

from decimal import Decimal
from re import compile as re_compile
from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel, Discriminator, Field, field_validator, model_validator,
)

_IDENT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_ALLOWED_OPS = (">", ">=", "<", "<=", "=", "!=")


class _NotNullParams(BaseModel):
    kind: Literal["not_null"]


class _UniqueParams(BaseModel):
    kind: Literal["unique"]


class _RangeParams(BaseModel):
    kind: Literal["range"]
    min: Decimal | None = None
    max: Decimal | None = None

    @model_validator(mode="after")
    def _atLeastOne(self) -> "_RangeParams":
        if self.min is None and self.max is None:
            raise ValueError("range 必须至少给 min 或 max 之一")
        return self


class _InSetParams(BaseModel):
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


class _RegexParams(BaseModel):
    kind: Literal["regex"]
    pattern: str = Field(min_length=1, max_length=500)

    @field_validator("pattern")
    @classmethod
    def _validatePattern(cls, v: str) -> str:
        try:
            re_compile(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"正则表达式非法: {exc}") from exc
        return v


class _CompareParams(BaseModel):
    kind: Literal["compare"]
    op: Literal[">", ">=", "<", "<=", "=", "!="]
    value: Decimal


class _RefParams(BaseModel):
    kind: Literal["ref"]
    ref_table: str = Field(pattern=_IDENT_PATTERN)
    ref_column: str = Field(pattern=_IDENT_PATTERN)


class _CrossColumnParams(BaseModel):
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


RuleParams = Annotated[
    Union[
        _NotNullParams, _UniqueParams, _RangeParams, _InSetParams,
        _RegexParams, _CompareParams, _RefParams, _CrossColumnParams,
    ],
    Discriminator("kind"),
]


class RuleParamsRead(BaseModel):
    """响应里回显的 params 形态：kind + 原始 payload（前端回填用）。"""
    kind: str
    raw: dict
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && pytest app/tests/unit/test_schemas_dq_rule_params.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/domain/schemas_dq_rule_params.py \
        backend/app/tests/unit/test_schemas_dq_rule_params.py
git commit -m "feat(dq): rule params Pydantic schemas (8 kinds)"
```

---

## Task 3: Compiler — 分发器 + 2 简单 kind（not_null, unique）

**Files:**
- Create: `backend/app/services/data_quality_evaluators/rule_params_compiler.py`
- Test: `backend/app/tests/unit/test_rule_params_compiler.py`

**Interfaces:**
- Consumes: `RuleType` enum, `RuleParams` discriminated union
- Produces: `compileRuleParams(ruleType, params) -> str` 入口 + `_compileNotNull/_compileUnique` 等 per-kind 函数

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/unit/test_rule_params_compiler.py
import pytest
from app.domain.enums import RuleType
from app.services.data_quality_evaluators.rule_params_compiler import (
    compileRuleParams, _compileNotNull, _compileUnique,
)
from app.services.data_quality_evaluators._common import validate_expression
from app.domain.models import DataQualityRule


def _rule(ruleType: RuleType, column: str = "PO_LINE_KEY") -> DataQualityRule:
    return DataQualityRule(
        id=1, rule_code="X", rule_name="X", rule_type=ruleType.value,
        target_table="PO_LINE", target_column=column,
    )


class TestCompileNotNull:
    def test_returns_is_not_null(self):
        rule = _rule(RuleType.COMPLETENESS, "ORDER_DATE")
        out = _compileNotNull(rule, {})
        assert out == "ORDER_DATE IS NOT NULL"

    def test_passes_whitelist(self):
        rule = _rule(RuleType.COMPLETENESS, "PO_LINE_KEY")
        validate_expression(_compileNotNull(rule, {}))


class TestCompileUnique:
    def test_returns_unique(self):
        rule = _rule(RuleType.UNIQUENESS, "PO_LINE_KEY")
        out = _compileUnique(rule, {})
        assert out == "UNIQUE(PO_LINE_KEY)"

    def test_passes_whitelist(self):
        rule = _rule(RuleType.UNIQUENESS, "PO_LINE_KEY")
        validate_expression(_compileUnique(rule, {}))


class TestDispatcher:
    def test_completeness_routes_to_not_null(self):
        rule = _rule(RuleType.COMPLETENESS, "PO_LINE_KEY")
        out = compileRuleParams(rule, {"kind": "not_null"})
        assert "IS NOT NULL" in out

    def test_uniqueness_routes_to_unique(self):
        rule = _rule(RuleType.UNIQUENESS, "PO_LINE_KEY")
        out = compileRuleParams(rule, {"kind": "unique"})
        assert "UNIQUE(" in out

    def test_unsupported_kind_raises(self):
        rule = _rule(RuleType.COMPLETENESS, "PO_LINE_KEY")
        with pytest.raises(ValueError):
            compileRuleParams(rule, {"kind": "in_set"})
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && pytest app/tests/unit/test_rule_params_compiler.py::TestCompileNotNull -v
```

Expected: FAIL（模块不存在）。

- [ ] **Step 3: 写最小实现**

```python
# backend/app/services/data_quality_evaluators/rule_params_compiler.py
"""规则结构化参数编译器（feat-dq-rule-params v1）。

纯函数无 IO；写入时调用，产物过 validate_expression 白名单。
evaluator 永远只读 data_quality_rule.rule_expression（无论结构化还是自定义），
本模块不参与评估期。
"""
from __future__ import annotations

from typing import Any, Callable

from app.domain.enums import RuleType
from app.domain.models import DataQualityRule
from app.services.data_quality_evaluators._common import quote_identifier


def _compileNotNull(rule: DataQualityRule, params: dict) -> str:
    col = quote_identifier(_adapter_stub(), rule.target_column or "")
    return f"{col} IS NOT NULL"


def _compileUnique(rule: DataQualityRule, params: dict) -> str:
    col = quote_identifier(_adapter_stub(), rule.target_column or "")
    return f"UNIQUE({col})"


def _adapter_stub():
    """编译器只需要 quote_identifier 的 dialect 分支；传 None adapter 走 PG 双引号默认。"""
    return None


_KIND_DISPATCH: dict[RuleType, dict[str, Callable[[DataQualityRule, dict], str]]] = {
    RuleType.COMPLETENESS: {"not_null": _compileNotNull},
    RuleType.UNIQUENESS: {"unique": _compileUnique},
}


def compileRuleParams(rule: DataQualityRule, params: dict[str, Any]) -> str:
    """按 (rule_type, kind) 路由到 _compile<Kind>。"""
    ruleType = RuleType(rule.rule_type)
    kind = params["kind"]
    fn = _KIND_DISPATCH.get(ruleType, {}).get(kind)
    if fn is None:
        raise ValueError(
            f"rule_type={ruleType.value} 不支持 kind={kind}（结构化参数）"
        )
    return fn(rule, params)


__all__ = ["compileRuleParams"]
```

注意：上面 `_adapter_stub` 仅为占位让编译产物保持一致；Task 4 起改为真正接收 adapter 参数（VALIDITY 等需要方言感知引号）。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && pytest app/tests/unit/test_rule_params_compiler.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/data_quality_evaluators/rule_params_compiler.py \
        backend/app/tests/unit/test_rule_params_compiler.py
git commit -m "feat(dq): rule params compiler (dispatcher + not_null/unique)"
```

---

## Task 4: Compiler — 4 种 VALIDITY kind（range / in_set / regex / compare）

**Files:**
- Modify: `backend/app/services/data_quality_evaluators/rule_params_compiler.py`
- Modify: `backend/app/tests/unit/test_rule_params_compiler.py`

**Interfaces:**
- Consumes: `_compileRange/_compileInSet/_compileRegex/_compileCompare(rule, params) -> str`
- Produces: VALIDITY 完整支持

- [ ] **Step 1: 追加失败测试**

```python
# 在 test_rule_params_compiler.py 追加
from app.services.data_quality_evaluators.rule_params_compiler import (
    _compileRange, _compileInSet, _compileRegex, _compileCompare,
)


class TestCompileRange:
    def test_min_max(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_QTY")
        assert _compileRange(rule, {"min": 0, "max": 100}) == '"ORDER_QTY" BETWEEN 0 AND 100'

    def test_min_only(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_QTY")
        assert _compileRange(rule, {"min": 0}) == '"ORDER_QTY" >= 0'

    def test_max_only(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_QTY")
        assert _compileRange(rule, {"max": 100}) == '"ORDER_QTY" <= 100'

    def test_whitelist(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_QTY")
        validate_expression(_compileRange(rule, {"min": 0, "max": 100}))


class TestCompileInSet:
    def test_basic(self):
        rule = _rule(RuleType.VALIDITY, "STATUS")
        out = _compileInSet(rule, {"values": ["A", "B"]})
        assert out == '"STATUS" IN (\'A\',\'B\')'

    def test_whitelist(self):
        rule = _rule(RuleType.VALIDITY, "STATUS")
        validate_expression(_compileInSet(rule, {"values": ["A"]}))


class TestCompileRegex:
    def test_basic(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_NO")
        out = _compileRegex(rule, {"pattern": "^[A-Z0-9]+$"})
        assert out == '"ORDER_NO" ~ \'^[A-Z0-9]+$\''

    def test_whitelist(self):
        rule = _rule(RuleType.VALIDITY, "ORDER_NO")
        validate_expression(_compileRegex(rule, {"pattern": "^[0-9]+$"}))


class TestCompileCompare:
    def test_gt(self):
        rule = _rule(RuleType.VALIDITY, "PRICE")
        assert _compileCompare(rule, {"op": ">", "value": 0}) == '"PRICE" > 0'

    def test_ne(self):
        rule = _rule(RuleType.VALIDITY, "FLAG")
        assert _compileCompare(rule, {"op": "!=", "value": 0}) == '"FLAG" != 0'

    def test_whitelist(self):
        rule = _rule(RuleType.VALIDITY, "PRICE")
        validate_expression(_compileCompare(rule, {"op": ">", "value": 0}))


class TestDispatcherValidity:
    def test_validity_routes(self):
        rule = _rule(RuleType.VALIDITY, "PRICE")
        assert "BETWEEN" in compileRuleParams(rule, {"kind": "range", "min": 0, "max": 100})
        assert "IN (" in compileRuleParams(rule, {"kind": "in_set", "values": ["A"]})
        assert "~" in compileRuleParams(rule, {"kind": "regex", "pattern": "^x$"})
        assert "> 0" in compileRuleParams(rule, {"kind": "compare", "op": ">", "value": 0})
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && pytest app/tests/unit/test_rule_params_compiler.py::TestCompileRange -v
```

Expected: FAIL。

- [ ] **Step 3: 实现 4 个 VALIDITY 编译函数**

替换 `rule_params_compiler.py` 中的 `_adapter_stub` 占位 + 加入 4 个 kind：

```python
from app.services.data_quality_evaluators._common import (
    quote_identifier, validate_identifier,
)


def _adapter_stub():
    """VALIDITY 不依赖方言分支；传 None adapter 走 PG 双引号默认。"""
    return None


def _colIdent(rule: DataQualityRule) -> str:
    if not rule.target_column:
        raise ValueError(f"rule_type={rule.rule_type} 需要 target_column")
    validate_identifier(rule.target_column, role="target_column")
    return quote_identifier(_adapter_stub(), rule.target_column)


def _compileRange(rule: DataQualityRule, params: dict) -> str:
    col = _colIdent(rule)
    lo, hi = params.get("min"), params.get("max")
    if lo is not None and hi is not None:
        return f"{col} BETWEEN {lo} AND {hi}"
    if lo is not None:
        return f"{col} >= {lo}"
    return f"{col} <= {hi}"


def _compileInSet(rule: DataQualityRule, params: dict) -> str:
    col = _colIdent(rule)
    values = params["values"]
    quoted = ",".join(f"'{v}'" for v in values)
    return f"{col} IN ({quoted})"


def _compileRegex(rule: DataQualityRule, params: dict) -> str:
    col = _colIdent(rule)
    return f"{col} ~ '{params['pattern']}'"


def _compileCompare(rule: DataQualityRule, params: dict) -> str:
    col = _colIdent(rule)
    return f"{col} {params['op']} {params['value']}"


_KIND_DISPATCH[RuleType.VALIDITY] = {
    "range": _compileRange,
    "in_set": _compileInSet,
    "regex": _compileRegex,
    "compare": _compileCompare,
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && pytest app/tests/unit/test_rule_params_compiler.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/data_quality_evaluators/rule_params_compiler.py \
        backend/app/tests/unit/test_rule_params_compiler.py
git commit -m "feat(dq): rule params compiler (4 VALIDITY kinds)"
```

---

## Task 5: Compiler — REFERENTIAL + CONSISTENCY

**Files:**
- Modify: `backend/app/services/data_quality_evaluators/rule_params_compiler.py`
- Modify: `backend/app/tests/unit/test_rule_params_compiler.py`

**Interfaces:**
- Consumes: `_compileRef/_compileCrossColumn(rule, params) -> str`
- Produces: REFERENTIAL 走 `REF tbl.col` 协议（与既有 `parse_ref_expression` 兼容）；CONSISTENCY 支持 `left op right × factor` 单子句

- [ ] **Step 1: 追加失败测试**

```python
class TestCompileRef:
    def test_basic(self):
        rule = _rule(RuleType.REFERENTIAL, "SUPPLIER_KEY")
        out = _compileRef(rule, {"ref_table": "SUPPLIER", "ref_column": "SUPPLIER_KEY"})
        assert out == "REF SUPPLIER.SUPPLIER_KEY"

    def test_whitelist(self):
        rule = _rule(RuleType.REFERENTIAL, "SUPPLIER_KEY")
        validate_expression(_compileRef(rule, {"ref_table": "SUPPLIER", "ref_column": "SUPPLIER_KEY"}))

    def test_invalid_identifier_raises(self):
        rule = _rule(RuleType.REFERENTIAL, "SUPPLIER_KEY")
        # Pydantic 拦截在前；这里直接构造非法字典验证防御
        with pytest.raises(Exception):
            _compileRef(rule, {"ref_table": "1BAD", "ref_column": "X"})


class TestCompileCrossColumn:
    def test_no_factor(self):
        rule = _rule(RuleType.CONSISTENCY, "RECEIVED_QTY")
        out = _compileCrossColumn(rule, {
            "left": "RECEIVED_QTY", "op": "<=", "right": "ORDER_QTY",
        })
        assert out == '"RECEIVED_QTY" <= "ORDER_QTY"'

    def test_with_factor(self):
        rule = _rule(RuleType.CONSISTENCY, "RECEIVED_QTY")
        out = _compileCrossColumn(rule, {
            "left": "RECEIVED_QTY", "op": "<=", "right": "ORDER_QTY", "factor": 1.05,
        })
        assert out == '"RECEIVED_QTY" <= "ORDER_QTY" * 1.05'

    def test_whitelist(self):
        rule = _rule(RuleType.CONSISTENCY, "RECEIVED_QTY")
        validate_expression(_compileCrossColumn(rule, {
            "left": "RECEIVED_QTY", "op": "<=", "right": "ORDER_QTY", "factor": 1.05,
        }))


class TestDispatcherFull:
    def test_referential(self):
        rule = _rule(RuleType.REFERENTIAL, "SUPPLIER_KEY")
        out = compileRuleParams(rule, {"ref_table": "SUPPLIER", "ref_column": "SUPPLIER_KEY"})
        assert out.startswith("REF ")

    def test_consistency(self):
        rule = _rule(RuleType.CONSISTENCY, "RECEIVED_QTY")
        out = compileRuleParams(rule, {
            "left": "RECEIVED_QTY", "op": "<=", "right": "ORDER_QTY", "factor": 1.05,
        })
        assert "RECEIVED_QTY" in out and "ORDER_QTY" in out
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && pytest app/tests/unit/test_rule_params_compiler.py::TestCompileRef -v
```

Expected: FAIL。

- [ ] **Step 3: 实现 ref / cross_column**

```python
def _compileRef(rule: DataQualityRule, params: dict) -> str:
    ref_table = validate_identifier(params["ref_table"], role="ref_table")
    ref_column = validate_identifier(params["ref_column"], role="ref_column")
    return f"REF {ref_table}.{ref_column}"


def _compileCrossColumn(rule: DataQualityRule, params: dict) -> str:
    left = validate_identifier(params["left"], role="left")
    right = validate_identifier(params["right"], role="right")
    op = params["op"]
    factor = params.get("factor")
    left_q = quote_identifier(_adapter_stub(), left)
    right_q = quote_identifier(_adapter_stub(), right)
    if factor is not None:
        return f"{left_q} {op} {right_q} * {factor}"
    return f"{left_q} {op} {right_q}"


_KIND_DISPATCH[RuleType.REFERENTIAL] = {"ref": _compileRef}
_KIND_DISPATCH[RuleType.CONSISTENCY] = {"cross_column": _compileCrossColumn}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && pytest app/tests/unit/test_rule_params_compiler.py -v
```

Expected: PASS（全部 5 个 kind 类）。

- [ ] **Step 5: Round-trip 白名单断言**

在 `test_rule_params_compiler.py` 末尾追加：

```python
@pytest.mark.parametrize("ruleType,params", [
    (RuleType.COMPLETENESS, {"kind": "not_null"}),
    (RuleType.UNIQUENESS, {"kind": "unique"}),
    (RuleType.VALIDITY, {"kind": "range", "min": 0, "max": 100}),
    (RuleType.VALIDITY, {"kind": "in_set", "values": ["A", "B"]}),
    (RuleType.VALIDITY, {"kind": "regex", "pattern": "^[0-9]+$"}),
    (RuleType.VALIDITY, {"kind": "compare", "op": ">", "value": 0}),
    (RuleType.REFERENTIAL, {"kind": "ref", "ref_table": "SUPPLIER", "ref_column": "SUPPLIER_KEY"}),
    (RuleType.CONSISTENCY, {"kind": "cross_column",
                            "left": "A", "op": "<=", "right": "B", "factor": 1.05}),
])
def test_all_kinds_pass_whitelist(ruleType, params):
    rule = _rule(ruleType, "PO_LINE_KEY")
    out = compileRuleParams(rule, params)
    validate_expression(out)
```

跑测试：

```bash
cd backend && pytest app/tests/unit/test_rule_params_compiler.py -v
```

Expected: PASS（8 个 parametrize 全绿）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/data_quality_evaluators/rule_params_compiler.py \
        backend/app/tests/unit/test_rule_params_compiler.py
git commit -m "feat(dq): rule params compiler (REFERENTIAL + CONSISTENCY + round-trip)"
```

---

## Task 6: Service 层 — CRUD + 编译编排

**Files:**
- Create: `backend/app/services/data_quality_rule_params_service.py`
- Test: `backend/app/tests/unit/test_data_quality_rule_params_service.py`

**Interfaces:**
- Consumes: `AsyncSession`, `DataQualityRuleParamsCreate/Update` DTO（来自 `schemas_dq_rule_params.py`，本任务补 DTO）
- Produces: `listParamsRules / getParamsRule / createParamsRule / updateParamsRule / deleteParamsRule`，互斥校验在 service 层兜底

- [ ] **Step 1: 在 `schemas_dq_rule_params.py` 追加 DTO（写入时编译）**

在 `RuleParamsRead` 之后追加：

```python
from app.domain.enums import RuleType, Severity
from datetime import datetime


class DataQualityRuleParamsCreate(BaseModel):
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
                raise ValueError(f"{self.rule_type.value} 规则必须提供 rule_params 或 rule_expression")
        if params is not None and expr is not None:
            raise ValueError("结构化模式忽略客户端 rule_expression；不要两者同时提供")
        return self


class DataQualityRuleParamsUpdate(BaseModel):
    rule_name: str | None = None
    threshold: Decimal | None = None
    severity: Severity | None = None
    target_column: str | None = Field(default=None, pattern=_IDENT_PATTERN)
    rule_params: dict | None = None
    rule_expression: str | None = None

    @model_validator(mode="after")
    def _mutex(self) -> "DataQualityRuleParamsUpdate":
        # 更新时不能把既有规则改成「既要 params 又要 expression」
        if self.rule_params is not None and self.rule_expression is not None:
            raise ValueError("结构化模式忽略客户端 rule_expression；不要两者同时提供")
        return self


class DataQualityRuleParamsRead(BaseModel):
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
```

- [ ] **Step 2: 写失败测试**

```python
# backend/app/tests/unit/test_data_quality_rule_params_service.py
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from app.domain.enums import RuleType, Severity
from app.domain.schemas_dq_rule_params import DataQualityRuleParamsCreate
from app.domain.exceptions import ValidationError, NotFoundError
from app.services.data_quality_rule_params_service import DataQualityRuleParamsService


@pytest.fixture
def session():
    s = AsyncMock()
    s.flush = AsyncMock()
    s.refresh = AsyncMock()
    return s


@pytest.fixture
def service(session):
    return DataQualityRuleParamsService(session)


class TestCreateStructured:
    async def test_validity_range_compiles_expression(self, service, session):
        dto = DataQualityRuleParamsCreate(
            rule_code="DQ_TEST", rule_name="test", rule_type=RuleType.VALIDITY,
            target_table="PO_LINE", target_column="ORDER_QTY",
            threshold=Decimal("95"), severity=Severity.MEDIUM, datasource_id=1,
            rule_params={"kind": "range", "min": 0, "max": 100},
        )
        # session.add 拿到调用参数，flush 后赋 id
        added = []
        session.add = lambda obj: added.append(obj)
        result = await service.create(dto)
        assert len(added) == 1
        rule = added[0]
        assert rule.rule_params == {"kind": "range", "min": 0, "max": 100}
        assert "BETWEEN" in rule.rule_expression
        assert "ORDER_QTY" in rule.rule_expression
        assert result.config_mode == "structured"


class TestCreateCustom:
    async def test_validity_expression_passthrough(self, service, session):
        dto = DataQualityRuleParamsCreate(
            rule_code="DQ_TEST2", rule_name="t", rule_type=RuleType.VALIDITY,
            target_table="PO_LINE", target_column="ORDER_QTY",
            threshold=Decimal("95"), severity=Severity.MEDIUM, datasource_id=1,
            rule_expression="ORDER_QTY > 0",
        )
        added = []
        session.add = lambda obj: added.append(obj)
        result = await service.create(dto)
        rule = added[0]
        assert rule.rule_expression == "ORDER_QTY > 0"
        assert rule.rule_params is None
        assert result.config_mode == "custom"


class TestMutex:
    def test_both_rejected_by_schema(self):
        with pytest.raises(Exception):  # pydantic ValidationError
            DataQualityRuleParamsCreate(
                rule_code="X", rule_name="x", rule_type=RuleType.VALIDITY,
                target_table="T", target_column="C",
                threshold=Decimal("95"), severity=Severity.LOW, datasource_id=1,
                rule_params={"kind": "range", "min": 0},
                rule_expression="C > 0",
            )

    def test_neither_rejected_by_schema(self):
        with pytest.raises(Exception):
            DataQualityRuleParamsCreate(
                rule_code="X", rule_name="x", rule_type=RuleType.VALIDITY,
                target_table="T", target_column="C",
                threshold=Decimal("95"), severity=Severity.LOW, datasource_id=1,
            )
```

- [ ] **Step 3: 跑测试确认失败**

```bash
cd backend && pytest app/tests/unit/test_data_quality_rule_params_service.py -v
```

Expected: FAIL。

- [ ] **Step 4: 实现 service**

```python
# backend/app/services/data_quality_rule_params_service.py
"""DQ 规则结构化参数 service（feat-dq-rule-params v1 隔离命名空间）。

写入时编译；存量规则零影响（走既有 data_quality_service 路径）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import RuleType
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataQualityRule
from app.domain.schemas_dq_rule_params import (
    DataQualityRuleParamsCreate, DataQualityRuleParamsRead,
    DataQualityRuleParamsUpdate,
)
from app.services.data_quality_evaluators.rule_params_compiler import compileRuleParams


def _toRead(rule: DataQualityRule) -> DataQualityRuleParamsRead:
    return DataQualityRuleParamsRead(
        id=rule.id,
        rule_code=rule.rule_code,
        rule_name=rule.rule_name,
        rule_type=RuleType(rule.rule_type),
        target_table=rule.target_table,
        target_column=rule.target_column,
        threshold=rule.threshold,
        severity=rule.severity,
        datasource_id=rule.datasource_id,
        rule_expression=rule.rule_expression,
        rule_params=rule.rule_params,
        config_mode="structured" if rule.rule_params else "custom",
    )


class DataQualityRuleParamsService:

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, *, datasource_id: int | None = None) -> list[DataQualityRuleParamsRead]:
        stmt = select(DataQualityRule)
        if datasource_id is not None:
            stmt = stmt.where(DataQualityRule.datasource_id == datasource_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_toRead(r) for r in rows]

    async def get(self, rule_id: int) -> DataQualityRuleParamsRead:
        rule = await self._session.get(DataQualityRule, rule_id)
        if rule is None:
            raise NotFoundError(f"规则 id={rule_id} 不存在")
        return _toRead(rule)

    async def create(self, dto: DataQualityRuleParamsCreate) -> DataQualityRuleParamsRead:
        rule = DataQualityRule(
            rule_code=dto.rule_code,
            rule_name=dto.rule_name,
            rule_type=dto.rule_type.value,
            target_table=dto.target_table,
            target_column=dto.target_column,
            threshold=dto.threshold,
            severity=dto.severity.value,
            datasource_id=dto.datasource_id,
            rule_params=dto.rule_params,
            rule_expression=self._compileIfStructured(dto.rule_params, dto.rule_type, dto.target_column)
            if dto.rule_params else dto.rule_expression,
        )
        self._session.add(rule)
        await self._session.flush()
        await self._session.refresh(rule)
        return _toRead(rule)

    async def update(self, rule_id: int, dto: DataQualityRuleParamsUpdate) -> DataQualityRuleParamsRead:
        rule = await self._session.get(DataQualityRule, rule_id)
        if rule is None:
            raise NotFoundError(f"规则 id={rule_id} 不存在")
        if dto.rule_name is not None:
            rule.rule_name = dto.rule_name
        if dto.threshold is not None:
            rule.threshold = dto.threshold
        if dto.severity is not None:
            rule.severity = dto.severity.value
        if dto.target_column is not None:
            rule.target_column = dto.target_column
        if dto.rule_params is not None:
            rule.rule_params = dto.rule_params
            rule.rule_expression = self._compileIfStructured(
                dto.rule_params, RuleType(rule.rule_type), rule.target_column,
            )
        elif dto.rule_expression is not None:
            rule.rule_params = None
            rule.rule_expression = dto.rule_expression
        await self._session.flush()
        await self._session.refresh(rule)
        return _toRead(rule)

    async def delete(self, rule_id: int) -> None:
        rule = await self._session.get(DataQualityRule, rule_id)
        if rule is None:
            raise NotFoundError(f"规则 id={rule_id} 不存在")
        await self._session.delete(rule)
        await self._session.flush()

    @staticmethod
    def _compileIfStructured(params: dict, ruleType: RuleType, targetColumn: str | None) -> str:
        """结构化模式：params → 编译产物；存 rule_expression（evaluator 读这列）。"""
        synthetic = DataQualityRule(
            id=0, rule_code="x", rule_name="x", rule_type=ruleType.value,
            target_table="x", target_column=targetColumn,
        )
        try:
            return compileRuleParams(synthetic, params)
        except Exception as exc:
            raise ValidationError(f"rule_params 编译失败: {exc}") from exc


__all__ = ["DataQualityRuleParamsService"]
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd backend && pytest app/tests/unit/test_data_quality_rule_params_service.py -v
```

Expected: PASS。

- [ ] **Step 6: 跑既有测试确认无回归**

```bash
cd backend && pytest app/tests/integration/test_data_quality_api.py -v
```

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add backend/app/domain/schemas_dq_rule_params.py \
        backend/app/services/data_quality_rule_params_service.py \
        backend/app/tests/unit/test_data_quality_rule_params_service.py
git commit -m "feat(dq): rule params service (CRUD + compile orchestration)"
```

---

## Task 7: API Router + 真实 PG 集成测试

**Files:**
- Create: `backend/app/api/v1/data_quality_rule_params.py`
- Test: `backend/app/tests/integration/test_data_quality_rule_params_api.py`

**Interfaces:**
- Consumes: FastAPI `APIRouter`, `DataQualityRuleParamsService`
- Produces: 5 个 endpoint（list/get/create/update/delete），路径 `/api/v1/dq-rule-params/rules`

- [ ] **Step 1: 写失败测试（真实 PG 集成）**

参考 `backend/app/tests/integration/test_data_quality_api.py` 顶部的 `client` / `getSessionFactory` fixture。

```python
# backend/app/tests/integration/test_data_quality_rule_params_api.py
import pytest
from decimal import Decimal
from httpx import AsyncClient


@pytest.mark.integration
class TestCreateStructured:

    async def test_validity_range_end_to_end(self, client: AsyncClient):
        payload = {
            "rule_code": "DQ_INT_RANGE",
            "rule_name": "order_qty range",
            "rule_type": "VALIDITY",
            "target_table": "PO_LINE",
            "target_column": "ORDER_QTY",
            "threshold": "95",
            "severity": "MEDIUM",
            "datasource_id": 1,
            "rule_params": {"kind": "range", "min": 0, "max": 100},
        }
        resp = await client.post("/api/v1/dq-rule-params/rules", json=payload)
        assert resp.status_code == 201
        body = resp.json()
        assert body["config_mode"] == "structured"
        assert body["rule_params"] == {"kind": "range", "min": 0, "max": 100}
        assert "BETWEEN" in body["rule_expression"]
        assert "ORDER_QTY" in body["rule_expression"]


@pytest.mark.integration
class TestCreateCustom:

    async def test_validity_expression(self, client: AsyncClient):
        payload = {
            "rule_code": "DQ_INT_CUSTOM",
            "rule_name": "custom",
            "rule_type": "VALIDITY",
            "target_table": "PO_LINE",
            "target_column": "ORDER_QTY",
            "threshold": "95",
            "severity": "LOW",
            "datasource_id": 1,
            "rule_expression": "ORDER_QTY > 0",
        }
        resp = await client.post("/api/v1/dq-rule-params/rules", json=payload)
        assert resp.status_code == 201
        body = resp.json()
        assert body["config_mode"] == "custom"
        assert body["rule_params"] is None
        assert body["rule_expression"] == "ORDER_QTY > 0"


@pytest.mark.integration
class TestMutex:

    async def test_both_rejected_422(self, client: AsyncClient):
        payload = {
            "rule_code": "DQ_BOTH", "rule_name": "x", "rule_type": "VALIDITY",
            "target_table": "T", "target_column": "C",
            "threshold": "95", "severity": "LOW", "datasource_id": 1,
            "rule_params": {"kind": "range", "min": 0},
            "rule_expression": "C > 0",
        }
        resp = await client.post("/api/v1/dq-rule-params/rules", json=payload)
        assert resp.status_code == 422

    async def test_neither_rejected_422(self, client: AsyncClient):
        payload = {
            "rule_code": "DQ_NEITHER", "rule_name": "x", "rule_type": "VALIDITY",
            "target_table": "T", "target_column": "C",
            "threshold": "95", "severity": "LOW", "datasource_id": 1,
        }
        resp = await client.post("/api/v1/dq-rule-params/rules", json=payload)
        assert resp.status_code == 422


@pytest.mark.integration
class TestListAndGet:

    async def test_list_empty(self, client: AsyncClient):
        resp = await client.get("/api/v1/dq-rule-params/rules")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && pytest app/tests/integration/test_data_quality_rule_params_api.py -v
```

Expected: FAIL（路由未注册 → 404）。

- [ ] **Step 3: 实现 router**

```python
# backend/app/api/v1/data_quality_rule_params.py
"""DQ 规则结构化参数 API（feat-dq-rule-params v1 隔离 router）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas_dq_rule_params import (
    DataQualityRuleParamsCreate, DataQualityRuleParamsRead,
    DataQualityRuleParamsUpdate,
)
from app.infrastructure.db import getSession
from app.services.data_quality_rule_params_service import (
    DataQualityRuleParamsService,
)

router = APIRouter(prefix="/dq-rule-params/rules", tags=["dq-rule-params"])


def _service(session: AsyncSession = Depends(getSession)) -> DataQualityRuleParamsService:
    return DataQualityRuleParamsService(session)


@router.get("", response_model=list[DataQualityRuleParamsRead])
async def listRules(
    datasource_id: int | None = Query(default=None),
    svc: DataQualityRuleParamsService = Depends(_service),
) -> list[DataQualityRuleParamsRead]:
    return await svc.list(datasource_id=datasource_id)


@router.get("/{rule_id}", response_model=DataQualityRuleParamsRead)
async def getRule(
    rule_id: int,
    svc: DataQualityRuleParamsService = Depends(_service),
) -> DataQualityRuleParamsRead:
    return await svc.get(rule_id)


@router.post("", response_model=DataQualityRuleParamsRead, status_code=status.HTTP_201_CREATED)
async def createRule(
    dto: DataQualityRuleParamsCreate,
    svc: DataQualityRuleParamsService = Depends(_service),
) -> DataQualityRuleParamsRead:
    return await svc.create(dto)


@router.put("/{rule_id}", response_model=DataQualityRuleParamsRead)
async def updateRule(
    rule_id: int,
    dto: DataQualityRuleParamsUpdate,
    svc: DataQualityRuleParamsService = Depends(_service),
) -> DataQualityRuleParamsRead:
    return await svc.update(rule_id, dto)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteRule(
    rule_id: int,
    svc: DataQualityRuleParamsService = Depends(_service),
) -> None:
    await svc.delete(rule_id)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && pytest app/tests/integration/test_data_quality_rule_params_api.py -v
```

Expected: PASS（注意：测试库需有 `datasource_id=1` 存在，否则 insert FK 失败；参考既有测试如何准备 datasource fixture）。

- [ ] **Step 5: 跑既有数据质量集成测试确认无回归**

```bash
cd backend && pytest app/tests/integration/test_data_quality_api.py \
                 app/tests/integration/test_data_quality_eval_api.py \
                 app/tests/integration/test_data_quality_score_api.py -v
```

Expected: PASS（既有评估路径完全不变）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/v1/data_quality_rule_params.py \
        backend/app/tests/integration/test_data_quality_rule_params_api.py
git commit -m "feat(dq): rule params API router + integration tests (real PG)"
```

---

## Task 8: 注册 router 到 main.py

**Files:**
- Modify: `backend/app/main.py:247-248` 区间（追加 import + include_router）

**Interfaces:**
- 在 `from app.api.v1 import (...)` 块按字母序加 `data_quality_rule_params,`
- 在 `app.include_router(...)` 块按字母序加 `data_quality_rule_params.router,` 一行

- [ ] **Step 1: 加 import**

在 `main.py` 第 247-248 行附近的 import 块按字母序插入（`data_quality` 与 `data_quality_generate` 之间）：

```python
        data_quality,
        data_quality_generate,
        data_quality_rule_params,   # ← 新增
        datasource,
```

- [ ] **Step 2: 加 include_router**

在第 291-293 行附近按字母序插入（`data_quality.router` 之后）：

```python
    app.include_router(data_quality.router, prefix="/api/v1/data-quality/rules", tags=["data-quality"])
    app.include_router(
        data_quality_rule_params.router,
        prefix="/api/v1",
        tags=["dq-rule-params"],
    )
```

- [ ] **Step 3: 启服务 + 冒烟**

```bash
cd backend && uvicorn app.main:app --reload &
sleep 3
curl -sS -X POST http://localhost:8000/api/v1/dq-rule-params/rules \
  -H 'content-type: application/json' \
  -d '{"rule_code":"SMOKE","rule_name":"smoke","rule_type":"VALIDITY","target_table":"PO_LINE","target_column":"ORDER_QTY","threshold":"95","severity":"MEDIUM","datasource_id":1,"rule_params":{"kind":"range","min":0,"max":100}}' | jq
```

Expected: 201，返回体含 `config_mode:"structured"`、`rule_expression` 含 `BETWEEN`。

- [ ] **Step 4: 跑全量后端测试**

```bash
cd backend && pytest -q
```

Expected: PASS（评估器集成测试全绿，证明零回归）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/main.py
git commit -m "feat(dq): register rule params router in main"
```

---

## Task 9: 前端 API client + 摘要工具 + i18n keys

**Files:**
- Create: `frontend/src/api/dataQualityRuleParams.ts`
- Create: `frontend/src/utils/ruleParamsSummary.ts`
- Modify: `frontend/src/i18n/zh-CN.ts` 与 `frontend/src/i18n/en-US.ts`（追加命名空间）
- Test: `frontend/src/tests/ruleParamsSummary.test.ts`

- [ ] **Step 1: 写摘要工具失败测试**

```typescript
// frontend/src/tests/ruleParamsSummary.test.ts
import { describe, expect, it } from "vitest";
import { summarizeRuleParams } from "../utils/ruleParamsSummary";

describe("summarizeRuleParams", () => {
  it("not_null", () => {
    expect(summarizeRuleParams({ kind: "not_null" }, "PO_LINE_KEY", "zh-CN"))
      .toBe("PO_LINE_KEY 非空");
  });

  it("unique", () => {
    expect(summarizeRuleParams({ kind: "unique" }, "PO_LINE_KEY", "zh-CN"))
      .toBe("PO_LINE_KEY 唯一");
  });

  it("range both", () => {
    expect(summarizeRuleParams(
      { kind: "range", min: 0, max: 100 }, "ORDER_QTY", "zh-CN",
    )).toBe("ORDER_QTY ∈ [0, 100]");
  });

  it("range min only", () => {
    expect(summarizeRuleParams({ kind: "range", min: 0 }, "QTY", "zh-CN"))
      .toBe("QTY ≥ 0");
  });

  it("in_set", () => {
    expect(summarizeRuleParams({ kind: "in_set", values: ["A", "B"] }, "STATUS", "zh-CN"))
      .toBe("STATUS ∈ {A, B}");
  });

  it("regex", () => {
    expect(summarizeRuleParams({ kind: "regex", pattern: "^[0-9]+$" }, "ORDER_NO", "zh-CN"))
      .toBe("ORDER_NO 匹配 ^[0-9]+$");
  });

  it("compare gt", () => {
    expect(summarizeRuleParams({ kind: "compare", op: ">", value: 0 }, "PRICE", "zh-CN"))
      .toBe("PRICE > 0");
  });

  it("ref", () => {
    expect(summarizeRuleParams(
      { kind: "ref", ref_table: "SUPPLIER", ref_column: "SUPPLIER_KEY" }, "SUPPLIER_KEY", "zh-CN",
    )).toBe("SUPPLIER_KEY 参照 SUPPLIER.SUPPLIER_KEY");
  });

  it("cross_column with factor", () => {
    expect(summarizeRuleParams({
      kind: "cross_column",
      left: "RECEIVED_QTY", op: "<=", right: "ORDER_QTY", factor: 1.05,
    }, "RECEIVED_QTY", "zh-CN")).toBe("RECEIVED_QTY ≤ ORDER_QTY × 1.05");
  });

  it("cross_column no factor", () => {
    expect(summarizeRuleParams({
      kind: "cross_column", left: "A", op: "=", right: "B",
    }, "A", "zh-CN")).toBe("A = B");
  });

  it("en locale", () => {
    expect(summarizeRuleParams({ kind: "not_null" }, "X", "en-US"))
      .toBe("X IS NOT NULL");
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npm test -- ruleParamsSummary.test.ts
```

Expected: FAIL。

- [ ] **Step 3: 实现摘要工具**

```typescript
// frontend/src/utils/ruleParamsSummary.ts
import type { RuleTypeLiteral } from "./ruleExpressionTemplates";

export type RuleParamsKind =
  | "not_null" | "unique" | "range" | "in_set" | "regex"
  | "compare" | "ref" | "cross_column";

export interface RuleParamsBase { kind: RuleParamsKind }
export interface RangeParams extends RuleParamsBase {
  kind: "range"; min?: number | string; max?: number | string;
}
export interface InSetParams extends RuleParamsBase {
  kind: "in_set"; values: string[];
}
export interface RegexParams extends RuleParamsBase {
  kind: "regex"; pattern: string;
}
export interface CompareParams extends RuleParamsBase {
  kind: "compare"; op: ">" | ">=" | "<" | "<=" | "=" | "!="; value: number | string;
}
export interface RefParams extends RuleParamsBase {
  kind: "ref"; ref_table: string; ref_column: string;
}
export interface CrossColumnParams extends RuleParamsBase {
  kind: "cross_column";
  left: string; op: ">" | ">=" | "<" | "<=" | "=" | "!=";
  right: string; factor?: number | string;
}
export type AnyRuleParams =
  | RuleParamsBase | RangeParams | InSetParams | RegexParams
  | CompareParams | RefParams | CrossColumnParams;

const OP_GLYPH: Record<string, string> = {
  ">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "=", "!=": "≠",
};

export function summarizeRuleParams(
  params: AnyRuleParams,
  columnHint: string,
  locale: "zh-CN" | "en-US",
): string {
  switch (params.kind) {
    case "not_null":
      return locale === "zh-CN" ? `${columnHint} 非空` : `${columnHint} IS NOT NULL`;
    case "unique":
      return locale === "zh-CN" ? `${columnHint} 唯一` : `${columnHint} IS UNIQUE`;
    case "range": {
      const { min, max } = params;
      if (min !== undefined && max !== undefined) {
        return locale === "zh-CN"
          ? `${columnHint} ∈ [${min}, ${max}]`
          : `${columnHint} BETWEEN ${min} AND ${max}`;
      }
      if (min !== undefined) {
        return locale === "zh-CN"
          ? `${columnHint} ≥ ${min}` : `${columnHint} >= ${min}`;
      }
      return locale === "zh-CN"
        ? `${columnHint} ≤ ${max}` : `${columnHint} <= ${max}`;
    }
    case "in_set": {
      const list = params.values.join(", ");
      return locale === "zh-CN"
        ? `${columnHint} ∈ {${list}}` : `${columnHint} IN (${list})`;
    }
    case "regex":
      return locale === "zh-CN"
        ? `${columnHint} 匹配 ${params.pattern}` : `${columnHint} MATCHES ${params.pattern}`;
    case "compare": {
      const op = OP_GLYPH[params.op] ?? params.op;
      return locale === "zh-CN"
        ? `${columnHint} ${op} ${params.value}` : `${columnHint} ${params.op} ${params.value}`;
    }
    case "ref":
      return locale === "zh-CN"
        ? `${columnHint} 参照 ${params.ref_table}.${params.ref_column}`
        : `${columnHint} REFERENCES ${params.ref_table}.${params.ref_column}`;
    case "cross_column": {
      const op = OP_GLYPH[params.op] ?? params.op;
      const factor = params.factor !== undefined
        ? (locale === "zh-CN" ? ` × ${params.factor}` : ` * ${params.factor}`)
        : "";
      return locale === "zh-CN"
        ? `${params.left} ${op} ${params.right}${factor}`
        : `${params.left} ${params.op} ${params.right}${factor}`;
    }
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd frontend && npm test -- ruleParamsSummary.test.ts
```

Expected: PASS。

- [ ] **Step 5: 实现 API client**

```typescript
// frontend/src/api/dataQualityRuleParams.ts
import type { RuleTypeLiteral } from "../utils/ruleExpressionTemplates";
import type { AnyRuleParams } from "../utils/ruleParamsSummary";

export interface RuleParamsReadDto {
  id: number;
  rule_code: string;
  rule_name: string;
  rule_type: RuleTypeLiteral;
  target_table: string;
  target_column: string | null;
  threshold: string;
  severity: string;
  datasource_id: number;
  rule_expression: string | null;
  rule_params: AnyRuleParams | null;
  config_mode: "structured" | "custom";
}

export interface RuleParamsCreateDto {
  rule_code: string;
  rule_name: string;
  rule_type: RuleTypeLiteral;
  target_table: string;
  target_column?: string | null;
  threshold: string;
  severity: string;
  datasource_id: number;
  rule_params?: AnyRuleParams | null;
  rule_expression?: string | null;
}

import { apiClient } from "./client";

export const ruleParamsApi = {
  list: (datasourceId?: number) =>
    apiClient.get<RuleParamsReadDto[]>("/api/v1/dq-rule-params/rules", {
      params: datasourceId ? { datasource_id: datasourceId } : undefined,
    }),
  get: (id: number) =>
    apiClient.get<RuleParamsReadDto>(`/api/v1/dq-rule-params/rules/${id}`),
  create: (dto: RuleParamsCreateDto) =>
    apiClient.post<RuleParamsReadDto>("/api/v1/dq-rule-params/rules", dto),
  update: (id: number, dto: Partial<RuleParamsCreateDto>) =>
    apiClient.put<RuleParamsReadDto>(`/api/v1/dq-rule-params/rules/${id}`, dto),
  remove: (id: number) =>
    apiClient.delete(`/api/v1/dq-rule-params/rules/${id}`),
};
```

如项目 `frontend/src/api/client.ts` 导出不同，参考 `frontend/src/api/dataQuality.ts` 的写法对齐。

- [ ] **Step 6: 加 i18n keys**

`frontend/src/i18n/zh-CN.ts` 追加命名空间：

```typescript
  "menu.item.dataQualityRuleParams": "数据质量 / 规则配置(结构化)",
  "dqRuleParams.title": "规则配置(结构化)",
  "dqRuleParams.create": "新建结构化规则",
  "dqRuleParams.mode.structured": "结构化",
  "dqRuleParams.mode.custom": "自定义 SQL",
  "dqRuleParams.fields.kind": "约束类型",
  "dqRuleParams.fields.min": "最小值",
  "dqRuleParams.fields.max": "最大值",
  "dqRuleParams.fields.values": "允许值",
  "dqRuleParams.fields.pattern": "正则",
  "dqRuleParams.fields.op": "运算符",
  "dqRuleParams.fields.value": "值",
  "dqRuleParams.fields.left": "左列",
  "dqRuleParams.fields.right": "右列",
  "dqRuleParams.fields.factor": "系数",
  "dqRuleParams.fields.refTable": "引用表",
  "dqRuleParams.fields.refColumn": "引用列",
```

`en-US.ts` 同步追加英文版：

```typescript
  "menu.item.dataQualityRuleParams": "Data Quality / Rule Config (Structured)",
  "dqRuleParams.title": "Rule Config (Structured)",
  "dqRuleParams.create": "New Structured Rule",
  "dqRuleParams.mode.structured": "Structured",
  "dqRuleParams.mode.custom": "Custom SQL",
  "dqRuleParams.fields.kind": "Constraint",
  "dqRuleParams.fields.min": "Min",
  "dqRuleParams.fields.max": "Max",
  "dqRuleParams.fields.values": "Allowed Values",
  "dqRuleParams.fields.pattern": "Pattern",
  "dqRuleParams.fields.op": "Operator",
  "dqRuleParams.fields.value": "Value",
  "dqRuleParams.fields.left": "Left Column",
  "dqRuleParams.fields.right": "Right Column",
  "dqRuleParams.fields.factor": "Factor",
  "dqRuleParams.fields.refTable": "Reference Table",
  "dqRuleParams.fields.refColumn": "Reference Column",
```

- [ ] **Step 7: 提交**

```bash
git add frontend/src/api/dataQualityRuleParams.ts \
        frontend/src/utils/ruleParamsSummary.ts \
        frontend/src/tests/ruleParamsSummary.test.ts \
        frontend/src/i18n/zh-CN.ts \
        frontend/src/i18n/en-US.ts
git commit -m "feat(dq): frontend api client + summary util + i18n keys"
```

---

## Task 10: RuleParamsForm 组件

**Files:**
- Create: `frontend/src/components/dq/RuleParamsForm.tsx`
- Test: `frontend/src/tests/RuleParamsForm.test.tsx`

**Interfaces:**
- Props: `{ ruleType, columns, value, onChange }`
- Columns 从 schema 缓存取（按现有 `RuleBatchStepColumns` 走同款 endpoint）
- 按 `ruleType + params.kind` 分发到子表单；输出受控 `AnyRuleParams`

- [ ] **Step 1: 写失败测试**

```tsx
// frontend/src/tests/RuleParamsForm.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import i18n from "../i18n";
import { RuleParamsForm } from "../components/dq/RuleParamsForm";

const wrap = (ui: React.ReactNode) => (
  <I18nextProvider i18n={i18n}>{ui}</I18nextProvider>
);

describe("RuleParamsForm - VALIDITY range", () => {
  it("renders min/max and updates value", () => {
    const onChange = vi.fn();
    render(wrap(
      <RuleParamsForm
        ruleType="VALIDITY"
        columns={[{ name: "ORDER_QTY" }]}
        value={null}
        onChange={onChange}
      />,
    ));
    // 选 kind = range（按 antd Select mousedown 触发，参考 vitest 经验笔记）
    // 简化：用默认 kind (range) 直接输入 min/max
    const minInput = screen.getByLabelText(/Min/i) as HTMLInputElement;
    fireEvent.change(minInput, { target: { value: "0" } });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "range", min: "0" }),
    );
  });
});

describe("RuleParamsForm - CONSISTENCY", () => {
  it("renders left/right select and factor", () => {
    render(wrap(
      <RuleParamsForm
        ruleType="CONSISTENCY"
        columns={[{ name: "RECEIVED_QTY" }, { name: "ORDER_QTY" }]}
        value={null}
        onChange={() => {}}
      />,
    ));
    expect(screen.getByText(/Left Column/i)).toBeInTheDocument();
    expect(screen.getByText(/Right Column/i)).toBeInTheDocument();
    expect(screen.getByText(/Factor/i)).toBeInTheDocument();
  });
});
```

具体 antd Select 触发（mousedown / click）的细节参考项目里 `frontend/src/tests/DataQualityPage.filters.test.tsx` 的现成做法（QA system 已知坑：mousedown 触发下拉）。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npm test -- RuleParamsForm.test.tsx
```

Expected: FAIL。

- [ ] **Step 3: 实现组件**

```tsx
// frontend/src/components/dq/RuleParamsForm.tsx
import { useTranslation } from "react-i18next";
import {
  Form, Input, InputNumber, Select, Switch, Tag,
} from "antd";
import { useMemo } from "react";
import type { AnyRuleParams, RuleParamsKind } from "../../utils/ruleParamsSummary";
import type { RuleTypeLiteral } from "../../utils/ruleExpressionTemplates";

interface ColumnLike { name: string; dataType?: string; }
interface Props {
  ruleType: RuleTypeLiteral;
  columns: ColumnLike[];
  value: AnyRuleParams | null;
  onChange: (v: AnyRuleParams) => void;
}

const KIND_OPTIONS_BY_RULE: Record<RuleTypeLiteral, { value: RuleParamsKind; labelKey: string }[]> = {
  COMPLETENESS: [{ value: "not_null", labelKey: "not_null" }],
  UNIQUENESS:   [{ value: "unique",   labelKey: "unique" }],
  VALIDITY:     [
    { value: "range",   labelKey: "range" },
    { value: "in_set",  labelKey: "in_set" },
    { value: "regex",   labelKey: "regex" },
    { value: "compare", labelKey: "compare" },
  ],
  REFERENTIAL:  [{ value: "ref",            labelKey: "ref" }],
  CONSISTENCY:  [{ value: "cross_column",   labelKey: "cross_column" }],
  TIMELINESS:   [],
};

export function RuleParamsForm({ ruleType, columns, value, onChange }: Props) {
  const { t } = useTranslation();
  const kindOptions = KIND_OPTIONS_BY_RULE[ruleType] ?? [];
  const currentKind = value?.kind ?? kindOptions[0]?.value;
  const columnOptions = useMemo(
    () => columns.map((c) => ({ value: c.name, label: c.name })),
    [columns],
  );

  function setKind(kind: RuleParamsKind) {
    const seed: AnyRuleParams = { kind } as AnyRuleParams;
    onChange(seed);
  }

  function patch(p: Partial<AnyRuleParams>) {
    onChange({ ...(value as AnyRuleParams), ...p } as AnyRuleParams);
  }

  if (!currentKind) return <div>{t("dqRuleParams.unsupported")}</div>;

  return (
    <Form layout="vertical">
      {kindOptions.length > 1 && (
        <Form.Item label={t("dqRuleParams.fields.kind")}>
          <Select
            value={currentKind}
            options={kindOptions.map((o) => ({ value: o.value, label: o.value }))}
            onChange={setKind}
          />
        </Form.Item>
      )}

      {currentKind === "range" && (
        <>
          <Form.Item label={t("dqRuleParams.fields.min")}>
            <InputNumber value={(value as any)?.min} onChange={(v) => patch({ min: v })} />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.max")}>
            <InputNumber value={(value as any)?.max} onChange={(v) => patch({ max: v })} />
          </Form.Item>
        </>
      )}

      {currentKind === "in_set" && (
        <Form.Item label={t("dqRuleParams.fields.values")}>
          <Select
            mode="tags"
            value={(value as any)?.values ?? []}
            onChange={(v) => patch({ values: v })}
          />
        </Form.Item>
      )}

      {currentKind === "regex" && (
        <Form.Item label={t("dqRuleParams.fields.pattern")}>
          <Input
            value={(value as any)?.pattern ?? ""}
            onChange={(e) => patch({ pattern: e.target.value })}
          />
        </Form.Item>
      )}

      {currentKind === "compare" && (
        <>
          <Form.Item label={t("dqRuleParams.fields.op")}>
            <Select
              value={(value as any)?.op ?? ">"}
              options={[">", ">=", "<", "<=", "=", "!="].map((o) => ({ value: o, label: o }))}
              onChange={(v) => patch({ op: v })}
            />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.value")}>
            <InputNumber value={(value as any)?.value} onChange={(v) => patch({ value: v })} />
          </Form.Item>
        </>
      )}

      {currentKind === "ref" && (
        <>
          <Form.Item label={t("dqRuleParams.fields.refTable")}>
            <Input
              value={(value as any)?.ref_table ?? ""}
              onChange={(e) => patch({ ref_table: e.target.value })}
            />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.refColumn")}>
            <Input
              value={(value as any)?.ref_column ?? ""}
              onChange={(e) => patch({ ref_column: e.target.value })}
            />
          </Form.Item>
        </>
      )}

      {currentKind === "cross_column" && (
        <>
          <Form.Item label={t("dqRuleParams.fields.left")}>
            <Select
              value={(value as any)?.left}
              options={columnOptions}
              onChange={(v) => patch({ left: v })}
            />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.op")}>
            <Select
              value={(value as any)?.op ?? "<="}
              options={[">", ">=", "<", "<=", "=", "!="].map((o) => ({ value: o, label: o }))}
              onChange={(v) => patch({ op: v })}
            />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.right")}>
            <Select
              value={(value as any)?.right}
              options={columnOptions}
              onChange={(v) => patch({ right: v })}
            />
          </Form.Item>
          <Form.Item label={t("dqRuleParams.fields.factor")}>
            <InputNumber
              value={(value as any)?.factor}
              onChange={(v) => patch({ factor: v })}
              placeholder="1"
            />
          </Form.Item>
        </>
      )}
    </Form>
  );
}
```

> 注：实现细节（Form.Item 命名、validation 触发等）以 `frontend/src/components/dq/RuleBatchStepColumns.tsx` 与 `frontend/src/pages/DataQualityRuleBatchCreatePage.tsx` 既有写法对齐。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd frontend && npm test -- RuleParamsForm.test.tsx
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/dq/RuleParamsForm.tsx \
        frontend/src/tests/RuleParamsForm.test.tsx
git commit -m "feat(dq): frontend RuleParamsForm component"
```

---

## Task 11: Page + 菜单 + 路由

**Files:**
- Create: `frontend/src/pages/DataQualityRuleParamsPage.tsx`
- Modify: `frontend/src/App.tsx`（追加路由）
- Modify: `backend/scripts/seed_menu_config.py`（追加菜单项）
- Test: `frontend/src/tests/DataQualityRuleParamsPage.test.tsx`

- [ ] **Step 1: 写失败测试**

```tsx
// frontend/src/tests/DataQualityRuleParamsPage.test.tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import i18n from "../i18n";
import { DataQualityRuleParamsPage } from "../pages/DataQualityRuleParamsPage";

describe("DataQualityRuleParamsPage", () => {
  it("renders title", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <DataQualityRuleParamsPage />
      </I18nextProvider>,
    );
    expect(screen.getByText(/Rule Config \(Structured\)/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && npm test -- DataQualityRuleParamsPage.test.tsx
```

Expected: FAIL。

- [ ] **Step 3: 实现 Page**

参考 `frontend/src/pages/DataQualityPage.tsx` 的页面结构（Table + 新建/编辑 Modal），但只覆盖结构化模式；自定义 SQL 模式仅一个 TextArea 兜底。

```tsx
// frontend/src/pages/DataQualityRuleParamsPage.tsx
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Button, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tag,
} from "antd";
import { ruleParamsApi, type RuleParamsCreateDto, type RuleParamsReadDto } from "../api/dataQualityRuleParams";
import { RuleParamsForm } from "../components/dq/RuleParamsForm";
import { summarizeRuleParams } from "../utils/ruleParamsSummary";

export function DataQualityRuleParamsPage() {
  const { t, i18n } = useTranslation();
  const [rows, setRows] = useState<RuleParamsReadDto[]>([]);
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const [mode, setMode] = useState<"structured" | "custom">("structured");

  async function refresh() {
    const { data } = await ruleParamsApi.list();
    setRows(data ?? []);
  }

  useEffect(() => { refresh(); }, []);

  async function onCreate() {
    const v = await form.validateFields();
    const dto: RuleParamsCreateDto = {
      ...v,
      rule_params: mode === "structured" ? v.rule_params : undefined,
      rule_expression: mode === "custom" ? v.rule_expression : undefined,
    };
    delete (dto as any).rule_params_dummy;
    await ruleParamsApi.create(dto);
    setOpen(false);
    form.resetFields();
    refresh();
  }

  return (
    <div style={{ padding: 24 }}>
      <h2>{t("dqRuleParams.title")}</h2>
      <Button type="primary" onClick={() => setOpen(true)}>
        {t("dqRuleParams.create")}
      </Button>
      <Table
        rowKey="id"
        dataSource={rows}
        columns={[
          { title: "Code", dataIndex: "rule_code" },
          { title: "Name", dataIndex: "rule_name" },
          { title: "Type", dataIndex: "rule_type" },
          {
            title: "Mode", dataIndex: "config_mode",
            render: (v) => <Tag color={v === "structured" ? "geekblue" : "default"}>{v}</Tag>,
          },
          {
            title: "Params",
            dataIndex: "rule_params",
            render: (params, row) => params
              ? summarizeRuleParams(params, row.target_column ?? row.target_table, i18n.language as any)
              : row.rule_expression,
          },
        ]}
      />
      <Modal open={open} onCancel={() => setOpen(false)} onOk={onCreate} title={t("dqRuleParams.create")} width={720}>
        <Space style={{ marginBottom: 16 }}>
          <span>{t("dqRuleParams.mode.structured")}</span>
          <Switch
            checked={mode === "custom"}
            onChange={(c) => setMode(c ? "custom" : "structured")}
          />
          <span>{t("dqRuleParams.mode.custom")}</span>
        </Space>
        <Form form={form} layout="vertical">
          <Form.Item name="rule_code" label="Code" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="rule_name" label="Name" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="rule_type" label="Type" rules={[{ required: true }]}>
            <Select options={["COMPLETENESS", "VALIDITY", "UNIQUENESS", "REFERENTIAL", "CONSISTENCY"].map((v) => ({ value: v, label: v }))} />
          </Form.Item>
          <Form.Item name="target_table" label="Target Table" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="target_column" label="Target Column"><Input /></Form.Item>
          <Form.Item name="datasource_id" label="Datasource ID" rules={[{ required: true }]}><InputNumber /></Form.Item>
          <Form.Item name="threshold" label="Threshold" rules={[{ required: true }]}><InputNumber /></Form.Item>
          <Form.Item name="severity" label="Severity" rules={[{ required: true }]}>
            <Select options={["LOW", "MEDIUM", "HIGH"].map((v) => ({ value: v, label: v }))} />
          </Form.Item>
          {mode === "structured" ? (
            <Form.Item name="rule_params" label="Rule Params">
              <RuleParamsForm
                ruleType={form.getFieldValue("rule_type") ?? "VALIDITY"}
                columns={[{ name: "ORDER_QTY" }, { name: "RECEIVED_QTY" }, { name: "PO_LINE_KEY" }]}
                value={null}
                onChange={(v) => form.setFieldsValue({ rule_params: v })}
              />
            </Form.Item>
          ) : (
            <Form.Item name="rule_expression" label="Rule Expression" rules={[{ required: true }]}>
              <Input.TextArea rows={3} />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </div>
  );
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd frontend && npm test -- DataQualityRuleParamsPage.test.tsx
```

Expected: PASS。

- [ ] **Step 5: 加路由（追加，不动既有）**

`frontend/src/App.tsx` 找到现有路由块，按字母序追加：

```tsx
import { DataQualityRuleParamsPage } from "./pages/DataQualityRuleParamsPage";

// 在 <Route> 列表追加：
<Route path="/data-quality/rule-params" element={<DataQualityRuleParamsPage />} />
```

具体位置参考既有路由写法（如有 `lazy` 包裹亦跟随一致）。

- [ ] **Step 6: 加菜单项**

`backend/scripts/seed_menu_config.py` 找到现有 `menu_config` 列表，按既有 schema 追加一条（参考项目已知 `menu.item` 命名空间约定）：

```python
{
    "code": "dataQualityRuleParams",
    "label_key": "menu.item.dataQualityRuleParams",
    "path": "/data-quality/rule-params",
    "parent_code": "<data_quality_parent_code>",
    "sort_order": <next>,
    "icon": "<同兄弟>",
},
```

具体父 code / sort_order / icon 按 `seed_menu_config.py` 既有菜单布局填写（参考紧邻 dataQuality 条目）。

- [ ] **Step 7: 跑 seed**

```bash
cd backend && python scripts/seed_menu_config.py
```

Expected: 写入成功，新菜单可见。

- [ ] **Step 8: 提交**

```bash
git add frontend/src/pages/DataQualityRuleParamsPage.tsx \
        frontend/src/tests/DataQualityRuleParamsPage.test.tsx \
        frontend/src/App.tsx \
        backend/scripts/seed_menu_config.py
git commit -m "feat(dq): rule params page + route + menu entry"
```

---

## Task 12: 最终验证

- [ ] **Step 1: 跑后端全量测试**

```bash
cd backend && pytest -q
```

Expected: PASS（既有 + 新增全绿）。

- [ ] **Step 2: 跑前端全量测试**

```bash
cd frontend && npm test -- --run
```

Expected: PASS。

- [ ] **Step 3: 跑后端覆盖率**

```bash
cd backend && pytest --cov=app/services/data_quality_evaluators/rule_params_compiler \
                   --cov=app/services/data_quality_rule_params_service \
                   --cov=app/api/v1/data_quality_rule_params \
                   --cov-report=term-missing
```

Expected: 每个新增模块覆盖率 ≥ 80%。

- [ ] **Step 4: 跑前端覆盖率**

```bash
cd frontend && npm run test:coverage
```

Expected: `ruleParamsSummary` / `RuleParamsForm` / `DataQualityRuleParamsPage` ≥ 80%。

- [ ] **Step 5: code-reviewer + security-reviewer**

按项目规则，写完代码立即审查：

```bash
# 由 Claude Code 自动启用
```

重点审查项：
- `rule_params_compiler` 注入面（每种 kind 产物必过白名单）
- `schemas_dq_rule_params` 字段级 ValidationError 路径准确性
- API 互斥校验在 Pydantic 层与 service 层双兜底
- alembic 0076 迁移可逆

- [ ] **Step 6: 真机部署冒烟（参考项目部署脚本）**

按 `Harness/rules/部署规范.md` 部署后：
1. 通过新菜单「数据质量 / 规则配置(结构化)」进入页面
2. 各 kind 建 1 条规则 → DB params/expr 落库正确 → 评估 PASS/FAIL 符合预期
3. 编辑某结构化规则 → 重编译产物稳定
4. 存量自定义规则评估行为不变（既有页面创建规则 + 评估）

- [ ] **Step 7: 最终提交（如有微调）**

```bash
git status
# 若有调整：
git add -A
git commit -m "chore(dq): rule params v1 verification fixes"
```

---

## 完成标准

- [ ] 12 个 task 全绿，提交历史清晰
- [ ] Alembic 0076 迁移可上可下，存量数据零损失
- [ ] 评估路径字节级回归（既有集成测试全绿）
- [ ] 后端 / 前端覆盖率 ≥ 80%（新增模块）
- [ ] code-reviewer + security-reviewer 审查通过
- [ ] 真机部署冒烟通过：5 类规则各 1 条结构化建 + 评估 + 编辑重编译稳定
- [ ] 二期方向（推导引擎 / LLM / 多子句 CONSISTENCY）单独记录

## 二期 TODO（v1 不做）

- 推导引擎 `_deriveForProperty` 各分支改为产 `rule_params`
- LLM parse-descriptions 路径直接产 params 写入 `data_quality_rule.rule_params`
- CONSISTENCY 多子句 AND/OR 结构化支持
- 跨数据源 schema 列选择器通用化（v1 限单数据源内）
