# 数据质量规则自动生成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从任意本体类（ontology_class）导入属性定义，自动生成默认数据质量规则（完整性/唯一性/引用性/有效性/跨表一致性），经预览确认后落库，供后续评估与评分使用。

**Architecture:** 纯函数推导引擎（`RuleSuggestionEngine`）消费"属性约束视图"，编排服务负责 schema 映射校验与幂等落库（`DataQualityRuleGenerateService`），LLM 仅做 description→候选约束建议（确认后沉淀回 `ontology_property.allowed_values`）。规则落 `data_quality_rule`（加 3 个溯源列），执行复用现有 5 维评估器。

**Tech Stack:** FastAPI + SQLAlchemy 2.x async + Alembic + PostgreSQL；前端 React + antd + axios + i18n（zh-CN/en-US）；测试 pytest + httpx（真实 PG）+ vitest。

**Spec:** `docs/superpowers/specs/2026-09-05-dq-rule-auto-generation-design.md`

## Global Constraints

- 不可变数据：DTO→新对象；ORM 更新遵循现有 service 模式（局部字段更新 + commit）。
- SQL 安全：生成的 rule_expression 必须过 `validate_expression` 白名单；业务查询只读。
- Token 计量：LLM 调用走 `createClient(None)`，计量由现有 token_counter 链路保证（测试断言）。
- TDD：先测后码；覆盖率 ≥ 80%（`pytest --cov=app --cov-fail-under=80`）。
- 文件 < 800 行、函数 < 50 行、嵌套 ≤ 4 层。
- Python 命名 camelCase（项目约定，非 PEP 8）；ORM/Pydantic 字段 snake_case。
- 集成测试必须真实 PG（`localhost:5433/qa_metadata_test`）+ 完整 HTTP 链路，禁止 sqlite + 直调 service。
- 评估器表达式契约：COMPLETENESS/UNIQUENESS 只用 `target_column`；REFERENTIAL 表达式 `REF <table>.<column>`；VALIDITY/CONSISTENCY 表达式是 SQL 谓词，嵌入 `SUM(CASE WHEN (expr) THEN 1 ELSE 0 END)`。
- rule_code 唯一约束 `uq_data_quality_rule_code`；rule_code pattern `^[A-Z][A-Z0-9_]*$`，≤100 字符。
- 菜单是 DB 驱动：`seed_menu_config.py` + i18n `menu.item.*` + `fallbackNav.ts` 三处同步。

---

### Task 1: 迁移 0044 + ORM 列 + DerivationType 枚举

**Files:**
- Create: `backend/alembic/versions/0044_dq_rule_auto_generation.py`
- Modify: `backend/app/domain/models.py`（`OntologyProperty` 约 271-309 行、`DataQualityRule` 约 799-848 行）
- Modify: `backend/app/domain/enums.py`（`RuleType` 后追加）
- Modify: `backend/app/domain/schemas.py`（`DataQualityRuleRead` 约 1641 行，加 3 字段）
- Test: `backend/app/tests/integration/test_dq_rule_auto_generation_migration.py`

**Interfaces:**
- Produces: `OntologyProperty.allowed_values`（`list[str] | None`）、`DataQualityRule.source_class_id` / `source_property_id` / `derivation_type`（均 nullable，`derivation_type` server_default `'MANUAL'`）、枚举 `DerivationType`。后续所有任务依赖这些列名。

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/integration/test_dq_rule_auto_generation_migration.py
"""迁移 0044 契约：新列存在、存量行 derivation_type=MANUAL、allowed_values 可写。"""
from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


async def test_dq_rule_derivation_columns_exist(dbSession):
    rows = (await dbSession.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'data_quality_rule' "
        "AND column_name IN ('source_class_id','source_property_id','derivation_type')"
    ))).scalars().all()
    assert set(rows) == {"source_class_id", "source_property_id", "derivation_type"}


async def test_dq_rule_derivation_type_defaults_manual(dbSession):
    from app.domain.models import DataQualityRule
    rule = DataQualityRule(
        rule_name="t", rule_code="T001", datasource_id=1, target_table="T",
        rule_type="COMPLETENESS", target_column="C",
    )
    dbSession.add(rule)
    await dbSession.commit()
    await dbSession.refresh(rule)
    assert rule.derivation_type == "MANUAL"
    assert rule.source_class_id is None


async def test_ontology_property_allowed_values_roundtrip(dbSession):
    from app.domain.models import OntologyClass, OntologyProperty
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty  # Task 4 提供
```

（第三条测试的 helper 依赖 Task 4，本任务先只保留前两条；`import` 行在 Task 4 补回。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest app/tests/integration/test_dq_rule_auto_generation_migration.py -v`
Expected: FAIL（列不存在）

- [ ] **Step 3: 写迁移 + ORM + 枚举 + DTO**

```python
# backend/alembic/versions/0044_dq_rule_auto_generation.py
"""dq rule auto-generation: ontology_property.allowed_values + rule 溯源三列

Revision ID: 0044_dq_rule_auto_generation
Revises: 0043_feature_rule_config
Why: spec §4 —— 字典值域沉淀 + 规则来源标记（幂等生成靠确定性 rule_code 去重）。
存量规则 derivation_type 由 server_default 回填 'MANUAL'。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_dq_rule_auto_generation"
down_revision = "0043_feature_rule_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ontology_property",
        sa.Column("allowed_values", sa.JSON(), nullable=True),
    )
    op.add_column(
        "data_quality_rule",
        sa.Column("source_class_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "data_quality_rule",
        sa.Column("source_property_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "data_quality_rule",
        sa.Column(
            "derivation_type",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'MANUAL'"),
        ),
    )
    op.create_index(
        "ix_dq_rule_source_class", "data_quality_rule", ["source_class_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_dq_rule_source_class", table_name="data_quality_rule")
    op.drop_column("data_quality_rule", "derivation_type")
    op.drop_column("data_quality_rule", "source_property_id")
    op.drop_column("data_quality_rule", "source_class_id")
    op.drop_column("ontology_property", "allowed_values")
```

models.py —— `OntologyProperty` 在 `source_column` 列后加：

```python
    # 值域型字典（feat-dq-rule-auto-generation）：LLM 建议确认后沉淀于此，
    # 推导引擎据此生成 VALIDITY 规则（表引用型字典仍走 ref_class_id）
    allowed_values: Mapped[list[str] | None] = mapped_column(
        JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
    )
```

`DataQualityRule` 在 `description` 列后加：

```python
    # 溯源（feat-dq-rule-auto-generation）：自动生成规则标记来源；
    # 存量/手工规则 derivation_type='MANUAL'
    source_class_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_property_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    derivation_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="MANUAL"
    )
```

（`__table_args__` 中追加 `Index("ix_dq_rule_source_class", "source_class_id")` 与两个 FK 约束可省——溯源列只做软引用，靠迁移中的裸列即可，避免删类时 RESTRICT 卡死。）

enums.py —— `Severity` 后追加：

```python
class DerivationType(str, Enum):
    """数据质量规则来源类型（feat-dq-rule-auto-generation spec §4）。"""

    PK_DERIVED = "PK_DERIVED"
    FK_DERIVED = "FK_DERIVED"
    DICT_REF = "DICT_REF"
    ALLOWED_VALUES = "ALLOWED_VALUES"
    NOT_NULL = "NOT_NULL"
    JOIN_CONSISTENCY = "JOIN_CONSISTENCY"
    LLM_DERIVED = "LLM_DERIVED"
    MANUAL = "MANUAL"
```

schemas.py —— `DataQualityRuleRead` 末尾加：

```python
    source_class_id: int | None = None
    source_property_id: int | None = None
    derivation_type: str | None = None
```

- [ ] **Step 4: 应用迁移 + 跑测试**

Run: `cd backend && alembic upgrade head && python -m pytest app/tests/integration/test_dq_rule_auto_generation_migration.py -v`
Expected: PASS（前两条）

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/0044_dq_rule_auto_generation.py backend/app/domain/models.py backend/app/domain/enums.py backend/app/domain/schemas.py backend/app/tests/integration/test_dq_rule_auto_generation_migration.py
git commit -m "feat(dq): 迁移0044 + 规则溯源列 + allowed_values"
```

---

### Task 2: 扩展表达式白名单（单引号字面量 + 正则运算符）

**Files:**
- Modify: `backend/app/services/data_quality_evaluators/_common.py:26`（`_EXPR_RE`）
- Test: `backend/app/tests/test_data_quality_expression_whitelist.py`（新建，单测目录与 `test_data_quality_evaluators.py` 同级）

**Interfaces:**
- Produces: `validate_expression` 接受 `COL IN ('A','B')`、`COL ~ '^-?[0-9]+$'`、`COL::text IS NOT NULL` 形态。Task 3 生成的表达式依赖此能力。

说明：现有白名单缺 `'` `~` `^` `$` `{}` `:`，导致值域/类型校验表达式无法生成。扩展后注入面不变——关键词黑名单（DROP/UNION/…）与只读 adapter 仍是防线，引号字面量无法逃逸 CASE WHEN 谓词上下文。

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/test_data_quality_expression_whitelist.py
"""表达式白名单扩展（feat-dq-rule-auto-generation）：值域字面量 + 正则 + 转型。"""
from __future__ import annotations

import pytest

from app.domain.exceptions import ValidationError
from app.services.data_quality_evaluators._common import validate_expression


@pytest.mark.parametrize("expr", [
    "STATUS IN ('NEW','CONFIRMED','CLOSED')",
    "STATUS IS NULL OR STATUS ~ '^-?[0-9]+$'",
    "AMT ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'",
    "COL::text IS NOT NULL",
    "FLAG IN ('true','false','t','f')",
])
def test_extended_expressions_pass(expr: str) -> None:
    assert validate_expression(expr) == expr


@pytest.mark.parametrize("expr", [
    "STATUS IN ('A'); DROP TABLE X",   # 含分号（不在白名单）
    "COL ~ 'x' UNION SELECT 1",        # UNION 黑名单
    "COL ~ 'x' -- comment",            # 注释
])
def test_injection_still_blocked(expr: str) -> None:
    with pytest.raises(ValidationError):
        validate_expression(expr)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest app/tests/test_data_quality_expression_whitelist.py -v`
Expected: FAIL（前 5 条过不了白名单）

- [ ] **Step 3: 改 `_EXPR_RE`**

```python
# 安全表达式：标识符 / 数字 / 比较算术 / 括号 / 小数点 / AND OR NOT / IS NULL /
# 单引号字面量（值域 IN 列表）/ POSIX 正则运算符 ~ ^ $ { } / :: 转型。
# 引号字面量与正则由生成端 sanitize（值内禁 '），黑名单 + 只读 adapter 仍是防线。
_EXPR_RE = re.compile(r"""^[A-Za-z0-9_\s\.\(\)\<\>\=\!\,\*\+\-\/"\':~\^\$\{\}]+$""")
```

- [ ] **Step 4: 跑新旧测试（确认无回归）**

Run: `cd backend && python -m pytest app/tests/test_data_quality_expression_whitelist.py app/tests/test_data_quality_evaluators.py app/tests/test_data_quality_evaluator_dispatcher.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/data_quality_evaluators/_common.py backend/app/tests/test_data_quality_expression_whitelist.py
git commit -m "feat(dq): 表达式白名单支持值域字面量与正则运算符"
```

---

### Task 3: 推导引擎（纯函数）

**Files:**
- Create: `backend/app/services/data_quality_rule_generator.py`
- Test: `backend/app/tests/test_data_quality_rule_generator.py`

**Interfaces:**
- Consumes: `RuleType` / `Severity`（enums）、Task 1 的 `DerivationType`。
- Produces（Task 4 依赖的确切签名）:

```python
@dataclass(frozen=True) class ClassContext: class_id, class_name, source_table, object_type
@dataclass(frozen=True) class PropertyMeta: property_id, property_name, source_column, data_type, is_primary_key, is_foreign_key, ref_class: ClassContext | None, allowed_values: list[str] | None
@dataclass(frozen=True) class ColumnMeta: column_name, data_type, nullable
@dataclass(frozen=True) class SchemaIndex: tables: dict[str, dict[str, ColumnMeta]]  # key 全大写
@dataclass(frozen=True) class JoinEdgeMeta: target_table, source_columns, target_columns, target_date_columns
@dataclass(frozen=True) class RuleSuggestion: rule_code, rule_name, rule_type: RuleType, target_table, target_column, rule_expression, threshold: Decimal, severity: Severity, derivation_type: DerivationType, source_property_id, confidence, reason
@dataclass(frozen=True) class BlockedProperty: property_name, reason
def buildRuleCode(className: str, propertyName: str, ruleType: RuleType) -> str
def deriveSuggestions(ctx: ClassContext, properties: list[PropertyMeta], joinEdges: list[JoinEdgeMeta], schemaIndex: SchemaIndex | None) -> tuple[list[RuleSuggestion], list[BlockedProperty]]
```

推导规则（spec §5 固化，默认值此处唯一确定）：

| 触发 | 规则 | threshold/severity | 置信度 |
|---|---|---|---|
| 物理列 nullable=False 且非 PK | COMPLETENESS | 100 / HIGH | HIGH |
| `is_primary_key` | UNIQUENESS | 100 / HIGH | HIGH |
| `is_foreign_key` + ref 类（非 Reference）有 source_table | REFERENTIAL `REF <ref表>.<ref主键列>` | 95 / MEDIUM | HIGH |
| ref 类 object_type=Reference 且有 source_table | VALIDITY `COL IN (SELECT <ref主键列> FROM <ref表>)` | 95 / MEDIUM | HIGH |
| `allowed_values` 非空 | VALIDITY `COL IN ('A','B')` | 95 / MEDIUM | HIGH |
| 本体 data_type 与物理列类型族不匹配（物理为文本族） | VALIDITY 正则（见下） | 95 / LOW | MEDIUM |
| join 边两侧存在同名 DATETIME 属性 | CONSISTENCY `EXISTS (SELECT 1 FROM <目标表> WHERE <目标表>.<目标键> = <源表>.<源键> AND <目标表>.<目标日期> >= <源表>.<源日期>)` | 95 / MEDIUM | MEDIUM |

类型正则（物理列类型含 char/text 时才生成）：INT→`^-?[0-9]+$`；DECIMAL→`^-?[0-9]+(\.[0-9]+)?$`；DATETIME→`^[0-9]{4}-[0-9]{2}-[0-9]{2}([ T][0-9]{2}:[0-9]{2}(:[0-9]{2})?)?`；BOOLEAN→`FLAG IN ('true','false','t','f','1','0')`。nullable 列表达式前缀 `COL IS NULL OR `。

- [ ] **Step 1: 写失败测试（表驱动，覆盖全部推导行 + 边界）**

```python
# backend/app/tests/test_data_quality_rule_generator.py
"""推导引擎单测（纯函数，无 IO）。"""
from __future__ import annotations

from decimal import Decimal

from app.domain.enums import DerivationType, RuleType, Severity
from app.services.data_quality_rule_generator import (
    ClassContext,
    ColumnMeta,
    JoinEdgeMeta,
    PropertyMeta,
    RuleSuggestion,
    SchemaIndex,
    buildRuleCode,
    deriveSuggestions,
)

CTX = ClassContext(class_id=1, class_name="PurchaseOrder", source_table="PORDER", object_type="Transaction")
SCHEMA = SchemaIndex(tables={
    "PORDER": {
        "PO_KEY": ColumnMeta("PO_KEY", "varchar", nullable=False),
        "STATUS": ColumnMeta("STATUS", "varchar", nullable=True),
        "SUPPLIER_KEY": ColumnMeta("SUPPLIER_KEY", "varchar", nullable=True),
        "PO_DATE": ColumnMeta("PO_DATE", "varchar", nullable=True),
        "AMOUNT": ColumnMeta("AMOUNT", "varchar", nullable=True),
    }
})


def test_build_rule_code_deterministic():
    assert buildRuleCode("PurchaseOrder", "po_key", RuleType.UNIQUENESS) == \
        "DQ_PURCHASEORDER_PO_KEY_UNIQUENESS"


def test_build_rule_code_truncates_with_hash_suffix():
    code = buildRuleCode("C" * 80, "P" * 60, RuleType.COMPLETENESS)
    assert len(code) <= 100 and code.startswith("DQ_")


def _prop(**kw):
    base = dict(property_id=10, property_name="po_key", source_column="PO_KEY",
                data_type="STRING", is_primary_key=True, is_foreign_key=False,
                ref_class=None, allowed_values=None)
    base.update(kw)
    return PropertyMeta(**base)


def test_pk_yields_uniqueness_and_completeness():
    sugg, blocked = deriveSuggestions(CTX, [_prop()], [], SCHEMA)
    types = {s.rule_type for s in sugg}
    assert types == {RuleType.UNIQUENESS, RuleType.COMPLETENESS}
    assert all(s.confidence == "HIGH" for s in sugg)
    assert blocked == []


def test_fk_to_non_reference_class_yields_referential():
    ref = ClassContext(class_id=2, class_name="Supplier", source_table="BPSUPPLIER", object_type="Master")
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="supplier_key", source_column="SUPPLIER_KEY",
        is_primary_key=False, is_foreign_key=True, ref_class=ref)], [], SCHEMA)
    ref_rules = [s for s in sugg if s.rule_type == RuleType.REFERENTIAL]
    assert len(ref_rules) == 1
    assert ref_rules[0].rule_expression == "REF BPSUPPLIER.SUPPLIER_KEY"
    assert ref_rules[0].derivation_type == DerivationType.FK_DERIVED


def test_ref_to_reference_class_yields_validity_dict_not_referential():
    ref = ClassContext(class_id=3, class_name="PoStatusDict", source_table="PO_STATUS", object_type="Reference")
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="status", source_column="STATUS",
        is_primary_key=False, is_foreign_key=True, ref_class=ref)], [], SCHEMA)
    validity = [s for s in sugg if s.rule_type == RuleType.VALIDITY]
    assert len(validity) == 1
    assert validity[0].rule_expression == "STATUS IN (SELECT STATUS FROM PO_STATUS)"
    assert validity[0].derivation_type == DerivationType.DICT_REF
    assert not [s for s in sugg if s.rule_type == RuleType.REFERENTIAL]


def test_allowed_values_yields_validity():
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="status", source_column="STATUS", is_primary_key=False,
        allowed_values=["NEW", "CONFIRMED"])], [], SCHEMA)
    v = [s for s in sugg if s.derivation_type == DerivationType.ALLOWED_VALUES]
    assert v[0].rule_expression == "STATUS IN ('NEW','CONFIRMED')"


def test_type_mismatch_yields_regex_validity():
    sugg, _ = deriveSuggestions(CTX, [_prop(
        property_name="amount", source_column="AMOUNT", data_type="DECIMAL",
        is_primary_key=False)], [], SCHEMA)
    v = [s for s in sugg if s.derivation_type == DerivationType.NOT_NULL]  # 不该有
    assert v == []
    regex_rules = [s for s in sugg if "AMOUNT ~" in (s.rule_expression or "")]
    assert len(regex_rules) == 1


def test_join_edge_yields_consistency():
    edge = JoinEdgeMeta(target_table="PORDERQ", source_columns=["PO_KEY"],
                        target_columns=["PO_KEY"], target_date_columns=["RECEIPT_DATE"])
    sugg, _ = deriveSuggestions(
        CTX, [_prop(property_name="po_date", source_column="PO_DATE",
                    data_type="DATETIME", is_primary_key=False)], [edge], SCHEMA)
    c = [s for s in sugg if s.rule_type == RuleType.CONSISTENCY]
    assert len(c) == 1
    assert c[0].rule_expression == (
        "EXISTS (SELECT 1 FROM PORDERQ WHERE PORDERQ.PO_KEY = PORDER.PO_KEY "
        "AND PORDERQ.RECEIPT_DATE >= PORDER.PO_DATE)"
    )


def test_missing_table_blocks_all():
    sugg, blocked = deriveSuggestions(CTX, [_prop()], [], None)
    assert sugg == []
    assert blocked[0].reason == "数据源 schema 未缓存"


def test_missing_column_blocks_property():
    sugg, blocked = deriveSuggestions(CTX, [_prop(source_column=None)], [], SCHEMA)
    assert sugg == []
    assert any(b.reason == "未配置物理列映射" for b in blocked)


def test_schema_missing_column_blocks_property():
    sugg, blocked = deriveSuggestions(
        CTX, [_prop(source_column="NOPE")], [], SCHEMA)
    assert sugg == []
    assert any("NOPE" in b.reason for b in blocked)


def test_allowed_value_with_quote_rejected():
    from app.domain.exceptions import ValidationError
    import pytest
    with pytest.raises(ValidationError):
        deriveSuggestions(CTX, [_prop(
            property_name="status", source_column="STATUS", is_primary_key=False,
            allowed_values=["OK'--"])], [], SCHEMA)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest app/tests/test_data_quality_rule_generator.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现引擎（<300 行）**

```python
# backend/app/services/data_quality_rule_generator.py
"""规则推导引擎（feat-dq-rule-auto-generation spec §3/§5）。

纯函数、无 IO：输入类上下文 + 属性约束视图 + schema 映射 + join 边，
输出规则建议与被阻断属性。升级方案 B（独立约束表）时仅替换装配
ConstraintProvider 的调用方，本引擎不动。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.enums import DerivationType, RuleType, Severity
from app.domain.exceptions import ValidationError

_RULE_CODE_MAX = 100
_ALLOWED_VALUE_RE = re.compile(r"^[^']{1,50}$")
_TYPE_PATTERNS: dict[str, str] = {
    "INT": r"^-?[0-9]+$",
    "DECIMAL": r"^-?[0-9]+(\.[0-9]+)?$",
    "DATETIME": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}([ T][0-9]{2}:[0-9]{2}(:[0-9]{2})?)?",
}
_PHYSICAL_TEXT_FAMILY = ("char", "text")


@dataclass(frozen=True)
class ClassContext:
    class_id: int
    class_name: str
    source_table: str | None
    object_type: str | None


@dataclass(frozen=True)
class PropertyMeta:
    property_id: int
    property_name: str
    source_column: str | None
    data_type: str
    is_primary_key: bool
    is_foreign_key: bool
    ref_class: ClassContext | None = None
    allowed_values: list[str] | None = None


@dataclass(frozen=True)
class ColumnMeta:
    column_name: str
    data_type: str
    nullable: bool


@dataclass(frozen=True)
class SchemaIndex:
    tables: dict[str, dict[str, ColumnMeta]] = field(default_factory=dict)


@dataclass(frozen=True)
class JoinEdgeMeta:
    target_table: str
    source_columns: list[str]
    target_columns: list[str]
    target_date_columns: list[str]


@dataclass(frozen=True)
class RuleSuggestion:
    rule_code: str
    rule_name: str
    rule_type: RuleType
    target_table: str
    target_column: str | None
    rule_expression: str | None
    threshold: Decimal
    severity: Severity
    derivation_type: DerivationType
    source_property_id: int | None
    confidence: str
    reason: str


@dataclass(frozen=True)
class BlockedProperty:
    property_name: str
    reason: str


def buildRuleCode(className: str, propertyName: str, ruleType: RuleType) -> str:
    """DQ_<CLASS>_<PROP>_<TYPE>；超长截断 + sha1 短后缀，保证 pattern 与唯一性倾向。"""
    raw = f"DQ_{_slug(className)}_{_slug(propertyName)}_{ruleType.value}"
    if len(raw) <= _RULE_CODE_MAX:
        return raw
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:7]
    return f"{raw[:_RULE_CODE_MAX - 8]}_{digest}"


def _slug(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()
    return out or "X"


def deriveSuggestions(
    ctx: ClassContext,
    properties: list[PropertyMeta],
    joinEdges: list[JoinEdgeMeta],
    schemaIndex: SchemaIndex | None,
) -> tuple[list[RuleSuggestion], list[BlockedProperty]]:
    """按 spec §5 推导映射表产出建议；返回 (建议, 被阻断属性)。"""
    suggestions: list[RuleSuggestion] = []
    blocked: list[BlockedProperty] = []
    if ctx.source_table is None:
        return [], [BlockedProperty(p.property_name, "类未配置 source_table") for p in properties]
    tableCols = schemaIndex.tables.get(ctx.source_table.upper()) if schemaIndex else None
    if tableCols is None:
        return [], [BlockedProperty(p.property_name, "数据源 schema 未缓存") for p in properties]

    for prop in properties:
        col = _resolveColumn(tableCols, prop.source_column)
        if col is None:
            blocked.append(BlockedProperty(
                prop.property_name,
                "未配置物理列映射" if prop.source_column is None
                else f"物理列不存在: {prop.source_column}",
            ))
            continue
        suggestions.extend(_deriveForProperty(ctx, prop, col))
    suggestions.extend(_deriveJoinConsistency(ctx, properties, joinEdges, tableCols))
    return suggestions, blocked
```

`_resolveColumn`（大写匹配）、`_deriveForProperty`（按推导表逐条 append，PK→UNIQUENESS+NOT NULL→COMPLETENESS、FK/字典/值域/类型正则 → VALIDITY 或 REFERENTIAL）、`_deriveJoinConsistency`（同名 DATETIME 属性配对 join 边）实现细节遵循测试契约；每个辅助函数 <50 行。值域含 `'` 时抛 `ValidationError("allowed_values 含非法字符: ...")`。

- [ ] **Step 4: 跑测试至全绿**

Run: `cd backend && python -m pytest app/tests/test_data_quality_rule_generator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/data_quality_rule_generator.py backend/app/tests/test_data_quality_rule_generator.py
git commit -m "feat(dq): 规则推导引擎（纯函数）"
```

---

### Task 4: GenerateService.preview + API 路由挂载

**Files:**
- Create: `backend/app/services/data_quality_rule_generate_service.py`
- Create: `backend/app/api/v1/data_quality_generate.py`
- Modify: `backend/app/main.py`（router 挂载块，约 256-263 行附近）、`backend/app/tests/_testapp.py`（同步挂载）
- Modify: `backend/app/domain/schemas.py`（新增 generate DTO）、`backend/app/services/messages_zh.py`（新增 MSG_DQ_GEN_*）
- Test: `backend/app/tests/integration/test_dq_rule_generate_api.py`

**Interfaces:**
- Consumes: Task 3 全部类型；`SchemaCache`/`OntologyClass`/`OntologyProperty`/`OntologyJoin`/`DataQualityRule` ORM；`OutboxService`。
- Produces:

```python
class DataQualityRuleGenerateService:
    async def preview(self, session, *, classId: int, datasourceId: int) -> GeneratePreviewResponse
    async def confirm(self, session, payload: GenerateConfirmRequest, actor: CurrentUser) -> GenerateConfirmResponse  # Task 5
    async def applySuggestion(self, session, payload: ApplySuggestionRequest, actor: CurrentUser) -> ApplySuggestionResponse  # Task 6
```

DTO（CamelModel，schemas.py 追加；响应字段 camelCase 自动转换）：

```python
class GeneratePreviewRequest(CamelModel):
    class_id: int = Field(..., gt=0)
    datasource_id: int = Field(..., gt=0)

class RuleSuggestionRead(CamelModel):
    rule_code: str
    rule_name: str
    rule_type: RuleType
    target_table: str
    target_column: str | None = None
    rule_expression: str | None = None
    threshold: Decimal
    severity: Severity
    derivation_type: DerivationType
    source_property_id: int | None = None
    confidence: str
    status: str  # NEW / EXISTS
    reason: str

class BlockedPropertyRead(CamelModel):
    property_name: str
    reason: str

class GeneratePreviewResponse(CamelModel):
    class_id: int
    class_name: str
    source_table: str | None = None
    datasource_id: int
    suggestions: list[RuleSuggestionRead] = Field(default_factory=list)
    blocked: list[BlockedPropertyRead] = Field(default_factory=list)
```

- [ ] **Step 1: 写失败集成测试**

```python
# backend/app/tests/integration/test_dq_rule_generate_api.py
"""自动生成 preview 集成测试：schema 三态映射 + EXISTS 幂等标记。"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio
GEN_BASE = "/api/v1/data-quality/rules/generate"


async def ensureClassWithProperty(dbSession: AsyncSession, *, sourceTable: str = "PORDER") -> int:
    """造一个本体类 + 一个 PK 属性，返回 class_id。"""
    from app.domain.models import OntologyClass, OntologyProperty
    import uuid
    suffix = uuid.uuid4().hex[:8]
    cls = OntologyClass(class_name=f"GenTestClass{suffix}", source_table=sourceTable,
                        object_type="Transaction")
    dbSession.add(cls)
    await dbSession.flush()
    dbSession.add(OntologyProperty(
        class_id=cls.id, property_name="po_key", source_column="PO_KEY", data_type="STRING",
        is_primary_key=True, is_foreign_key=False))
    await dbSession.commit()
    return cls.id


async def ensureDataSourceAndSchema(dbSession, *, tables: dict) -> int:
    """造数据源 + schema_cache（tables: {表名: [(列名, 类型, nullable)]}）。"""
    from app.domain.models import DataSource, SchemaCache
    import uuid
    from app.infrastructure.security import crypto
    ds = DataSource(name=f"gen-{uuid.uuid4().hex[:8]}", type="POSTGRESQL", host="localhost",
                   port=5433, database_name="qa_metadata_test", username="qa_user",
                   password_encrypted=crypto.encryptPassword("x"))
    dbSession.add(ds)
    await dbSession.flush()
    schemaData = [
        {"table_name": t, "owner": "", "columns": [
            {"column_name": c, "data_type": ty, "nullable": nul} for c, ty, nul in cols],
         "primary_keys": [], "foreign_keys": []}
        for t, cols in tables.items()
    ]
    dbSession.add(SchemaCache(datasource_id=ds.id, schema_data=schemaData, schema_version="v1"))
    await dbSession.commit()
    return ds.id


async def test_preview_matched_yields_suggestions(client: AsyncClient, dbSession: AsyncSession):
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(dbSession, tables={
        "PORDER": [("PO_KEY", "varchar", False)]})
    res = await client.post(f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId})
    assert res.status_code == 200
    body = res.json()
    assert body["className"].startswith("GenTestClass")
    types = {s["ruleType"] for s in body["suggestions"]}
    assert types == {"UNIQUENESS", "COMPLETENESS"}
    assert all(s["status"] == "NEW" for s in body["suggestions"])
    assert body["blocked"] == []


async def test_preview_missing_column_blocks(client, dbSession):
    classId = await ensureClassWithProperty(dbSession, sourceTable="PORDER")
    dsId = await ensureDataSourceAndSchema(dbSession, tables={"PORDER": [("OTHER", "varchar", True)]})
    res = await client.post(f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId})
    body = res.json()
    assert body["suggestions"] == []
    assert any("PO_KEY" in b["reason"] for b in body["blocked"])


async def test_preview_no_schema_cache_blocks_all(client, dbSession):
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(dbSession, tables={})
    res = await client.post(f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId})
    body = res.json()
    assert body["suggestions"] == []
    assert all(b["reason"] == "数据源 schema 未缓存" for b in body["blocked"])


async def test_preview_class_not_found(client: AsyncClient):
    res = await client.post(f"{GEN_BASE}/preview", json={"classId": 999999, "datasourceId": 1})
    assert res.status_code == 404


async def test_preview_existing_rule_marked_exists(client, dbSession):
    from app.domain.models import DataQualityRule
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(dbSession, tables={
        "PORDER": [("PO_KEY", "varchar", False)]})
    dbSession.add(DataQualityRule(
        rule_name="既有", rule_code="DQ_GENTESTCLASS_PO_KEY_UNIQUENESS", datasource_id=dsId,
        target_table="PORDER", target_column="PO_KEY", rule_type="UNIQUENESS"))
    await dbSession.commit()
    res = await client.post(f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId})
    uniq = [s for s in res.json()["suggestions"] if s["ruleType"] == "UNIQUENESS"][0]
    assert uniq["status"] == "EXISTS"
```

（`ensureClassWithProperty` 用 uuid 后缀避免 (class_name, version) 唯一冲突；`DQ_GENTESTCLASS_PO_KEY_UNIQUENESS` 的 code 匹配 `buildRuleCode` 产出——执行时若与实际 code 不符，以引擎实际产出为准修正测试。）

- [ ] **Step 2: 跑测试确认失败（404 无路由）**

Run: `cd backend && python -m pytest app/tests/integration/test_dq_rule_generate_api.py -v`
Expected: FAIL

- [ ] **Step 3: 实现 service.preview + 路由 + DTO + 挂载**

service 关键逻辑：

```python
# backend/app/services/data_quality_rule_generate_service.py（preview 部分）
async def preview(self, session, *, classId: int, datasourceId: int) -> GeneratePreviewResponse:
    cls = await session.get(OntologyClass, classId)
    if cls is None:
        raise NotFoundError(MSG_DQ_GEN_CLASS_NOT_FOUND.format(id=classId))
    props = (await session.execute(
        select(OntologyProperty).where(OntologyProperty.class_id == classId)
    )).scalars().all()
    refIds = {p.ref_class_id for p in props if p.ref_class_id}
    refClasses = {c.id: c for c in (await session.execute(
        select(OntologyClass).where(OntologyClass.id.in_(refIds)))).scalars()} if refIds else {}
    joins = (await session.execute(
        select(OntologyJoin).where(OntologyJoin.source_class_id == classId))).scalars().all()
    cache = await SchemaIntrospectionService().getCached(session, datasourceId)
    schemaIndex = _buildSchemaIndex(cache.schema_data) if cache else None
    ctx = ClassContext(cls.id, cls.class_name, cls.source_table, cls.object_type)
    # PropertyMeta 装配（ConstraintProvider 职责，升级 B 时替换此处）
    metas = [_toPropertyMeta(p, refClasses) for p in props]
    joinEdges = [_toJoinEdge(j, cls) for j in joins]
    suggestions, blocked = deriveSuggestions(ctx, metas, joinEdges, schemaIndex)
    existing = set((await session.execute(
        select(DataQualityRule.rule_code).where(
            DataQualityRule.rule_code.in_([s.rule_code for s in suggestions]))
    )).scalars().all())
    # status: NEW → EXISTS（不可变：重建 list）
    return GeneratePreviewResponse(...)
```

`_buildSchemaIndex`：`{t["table_name"].upper(): {c["column_name"].upper(): ColumnMeta(...) for c in t["columns"]} for t in schemaData}`。
`_toJoinEdge`：解析 `ontology_join` 行 → `JoinEdgeMeta`（target class 的 source_table + 双方 DATETIME 属性的物理列配对；无可配对日期列则 `target_date_columns=[]`，引擎跳过）。
类无属性：200 + 空 suggestions（前端提示）。

路由（`data_quality_generate.py`）：

```python
router = APIRouter(prefix="/api/v1/data-quality/rules/generate", tags=["data-quality-generate"])

@router.post("/preview", response_model=GeneratePreviewResponse)
async def previewRules(
    payload: GeneratePreviewRequest,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> GeneratePreviewResponse:
    return await DataQualityRuleGenerateService().preview(
        session, classId=payload.class_id, datasourceId=payload.datasource_id)
```

`main.py` 挂载块加 `from app.api.v1.data_quality_generate import router as dq_generate_router` + `app.include_router(dq_generate_router)`；`_testapp.py` 同步（对照现有 data_quality 两条 router 的写法）。

- [ ] **Step 4: 跑测试至全绿 + 补回 Task 1 第三条测试**

Run: `cd backend && python -m pytest app/tests/integration/test_dq_rule_generate_api.py app/tests/integration/test_dq_rule_auto_generation_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/data_quality_rule_generate_service.py backend/app/api/v1/data_quality_generate.py backend/app/main.py backend/app/tests/_testapp.py backend/app/domain/schemas.py backend/app/services/messages_zh.py backend/app/tests/integration/test_dq_rule_generate_api.py
git commit -m "feat(dq): 生成 preview API（schema 映射 + EXISTS 幂等标记）"
```

---

### Task 5: GenerateService.confirm（批量落库 + 审计 + 幂等跳过）

**Files:**
- Modify: `backend/app/services/data_quality_rule_generate_service.py`、`backend/app/api/v1/data_quality_generate.py`、`backend/app/domain/schemas.py`、`backend/app/services/messages_zh.py`
- Test: 追加到 `backend/app/tests/integration/test_dq_rule_generate_api.py`

**Interfaces:**
- Consumes: Task 4 的路由与 service；`OutboxService.enqueue(session, event_type=..., entity_type=..., entity_id=..., actor=..., actor_departments=..., payload=...)`。
- Produces: `POST /confirm`，DTO：

```python
class GenerateRuleItem(CamelModel):
    rule_code: str = Field(..., min_length=1, max_length=100, pattern=r"^[A-Z][A-Z0-9_]*$")
    rule_name: str = Field(..., min_length=1, max_length=100)
    target_table: str = Field(..., min_length=1, max_length=100)
    target_column: str | None = Field(default=None, max_length=100)
    rule_type: RuleType
    rule_expression: str | None = None
    threshold: Decimal = Field(default=Decimal("95.00"), ge=0, le=100)
    severity: Severity = Severity.MEDIUM
    source_class_id: int | None = None
    source_property_id: int | None = None
    derivation_type: DerivationType = DerivationType.MANUAL
    description: str | None = None

class GenerateConfirmRequest(CamelModel):
    datasource_id: int = Field(..., gt=0)
    rules: list[GenerateRuleItem] = Field(..., min_length=1, max_length=500)

class GenerateConfirmResponse(CamelModel):
    created: list[DataQualityRuleRead] = Field(default_factory=list)
    skipped_codes: list[str] = Field(default_factory=list)
```

- [ ] **Step 1: 写失败测试**

```python
async def test_confirm_creates_rules_with_owner_and_audit(client, dbSession):
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(dbSession, tables={
        "PORDER": [("PO_KEY", "varchar", False)]})
    preview = (await client.post(f"{GEN_BASE}/preview", json={
        "classId": classId, "datasourceId": dsId})).json()
    rules = [dict(s, description=None) for s in preview["suggestions"] if s["status"] == "NEW"]
    res = await client.post(f"{GEN_BASE}/confirm", json={"datasourceId": dsId, "rules": rules})
    assert res.status_code == 200
    assert len(res.json()["created"]) == len(rules)
    assert res.json()["skippedCodes"] == []
    # 溯源列 + owner 派生 + 审计 outbox
    from sqlalchemy import text
    rows = (await dbSession.execute(text(
        "SELECT rule_code, source_class_id, derivation_type FROM data_quality_rule "
        "WHERE rule_code LIKE 'DQ_GENTESTCLASS%'"))).fetchall()
    assert all(r.source_class_id == classId for r in rows)
    outbox = (await dbSession.execute(text(
        "SELECT count(*) FROM audit_outbox WHERE event_type = 'data_quality_rule_created'"))).scalar()
    assert outbox == len(rules)


async def test_confirm_repeat_skips_existing(client, dbSession):
    # 同 preview 连续 confirm 两次，第二次 created=[] 且 skippedCodes 非空
    ...（同上造数，confirm 两次断言）


async def test_confirm_duplicate_code_in_batch_counts_skipped(client, dbSession):
    # rules 列表内含重复 rule_code（不同 datasource 下已存在的 code）→ skippedCodes
    ...
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest app/tests/integration/test_dq_rule_generate_api.py -v`
Expected: 新增用例 FAIL

- [ ] **Step 3: 实现 confirm**

```python
async def confirm(self, session, payload, actor) -> GenerateConfirmResponse:
    created: list[DataQualityRule] = []
    skipped: list[str] = []
    existing = set((await session.execute(
        select(DataQualityRule.rule_code).where(
            DataQualityRule.rule_code.in_([r.rule_code for r in payload.rules]))
    )).scalars().all())
    for item in payload.rules:
        if item.rule_code in existing:
            skipped.append(item.rule_code)
            continue
        entity = DataQualityRule(
            rule_name=item.rule_name, rule_code=item.rule_code,
            datasource_id=payload.datasource_id, target_table=item.target_table,
            target_column=item.target_column, rule_type=item.rule_type,
            rule_expression=item.rule_expression, threshold=item.threshold,
            severity=item.severity, owner=actor.departments[0] if actor.departments else None,
            description=item.description, source_class_id=item.source_class_id,
            source_property_id=item.source_property_id,
            derivation_type=item.derivation_type.value)
        try:
            async with session.begin_nested():
                session.add(entity)
                await session.flush()
        except IntegrityError:
            skipped.append(item.rule_code)  # 并发撞唯一约束 → 记跳过不失败批次
            continue
        created.append(entity)
        await self._outbox.enqueue(
            session, event_type="data_quality_rule_created", entity_type="data_quality_rule",
            entity_id=entity.id, actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"derivation_type": item.derivation_type.value,
                     "source_class_id": item.source_class_id})
        existing.add(item.rule_code)
    await session.commit()
    return GenerateConfirmResponse(
        created=[DataQualityRuleRead.model_validate(r, from_attributes=True) for r in created],
        skipped_codes=skipped)
```

路由 `POST /confirm`（`getCurrentUser`，与 createRule 权限一致）。

- [ ] **Step 4: 跑测试至全绿**

Run: `cd backend && python -m pytest app/tests/integration/test_dq_rule_generate_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A backend/app/services/data_quality_rule_generate_service.py backend/app/api/v1/data_quality_generate.py backend/app/domain/schemas.py backend/app/services/messages_zh.py backend/app/tests/integration/test_dq_rule_generate_api.py
git commit -m "feat(dq): 生成 confirm 批量落库 + outbox 审计 + 幂等跳过"
```

---

### Task 6: LLM parse-descriptions + apply-suggestion（建议沉淀闭环）

**Files:**
- Create: `backend/app/services/data_quality_rule_llm_service.py`
- Modify: `backend/app/services/data_quality_rule_generate_service.py`（applySuggestion）、`backend/app/api/v1/data_quality_generate.py`、`backend/app/domain/schemas.py`、`backend/app/services/messages_zh.py`
- Test: `backend/app/tests/integration/test_dq_rule_generate_llm_api.py`

**Interfaces:**
- Consumes: `createClient(None)`（`app.infrastructure.llm.factory`）、`LLMUnavailableError`、Task 5 的 OutboxService 模式。测试注入 LLM fake 的方式参照 `test_feature_rule_llm_parse.py` 的现有做法（阅读该文件后照抄其 fake 注入路径——若其通过 monkeypatch `createClient`，则同样处理）。
- Produces:

```python
# DTO
class ParseDescriptionsRequest(CamelModel):
    class_id: int = Field(..., gt=0)

class PropertyConstraintSuggestionRead(CamelModel):
    property_id: int
    property_name: str
    kind: str  # allowed_values | not_null
    values: list[str] | None = None
    confidence: float
    rationale: str

class ParseDescriptionsResponse(CamelModel):
    suggestions: list[PropertyConstraintSuggestionRead] = Field(default_factory=list)

class ApplySuggestionRequest(CamelModel):
    property_id: int = Field(..., gt=0)
    allowed_values: list[str] = Field(..., min_length=1, max_length=50)

class ApplySuggestionResponse(CamelModel):
    property_id: int
    allowed_values: list[str]
```

```python
# data_quality_rule_llm_service.py
async def parsePropertyDescriptions(
    session: AsyncSession, payload: ParseDescriptionsRequest, llm_client: Any, actor: str | None = None,
) -> ParseDescriptionsResponse
```

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/integration/test_dq_rule_generate_llm_api.py
"""parse-descriptions（LLM fake 注入 + token 计量）与 apply-suggestion 沉淀闭环。"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio
GEN_BASE = "/api/v1/data-quality/rules/generate"


class _FakeCompletion:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLMClient:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []
    async def complete(self, messages):
        self.calls.append(messages)
        return _FakeCompletion(self._content)


LLM_JSON = """{"suggestions": [
    {"property_name": "status", "kind": "allowed_values",
     "values": ["NEW", "CONFIRMED"], "confidence": 0.8, "rationale": "描述中列出取值"}
]}"""


async def test_parse_descriptions_returns_suggestions(client, dbSession, monkeypatch):
    from app.tests.integration.test_dq_rule_generate_api import ensureClassWithProperty
    classId = await ensureClassWithProperty(dbSession)
    fake = _FakeLLMClient(LLM_JSON)
    monkeypatch.setattr("app.api.v1.data_quality_generate.createClient", lambda _: fake)
    res = await client.post(f"{GEN_BASE}/parse-descriptions", json={"classId": classId})
    assert res.status_code == 200
    sugg = res.json()["suggestions"]
    assert sugg[0]["values"] == ["NEW", "CONFIRMED"]


async def test_parse_descriptions_llm_down_returns_503(client, dbSession, monkeypatch):
    class _Boom:
        async def complete(self, messages):
            raise RuntimeError("llm down")
    monkeypatch.setattr("app.api.v1.data_quality_generate.createClient", lambda _: _Boom())
    classId = await ensureClassWithProperty(dbSession)
    res = await client.post(f"{GEN_BASE}/parse-descriptions", json={"classId": classId})
    assert res.status_code == 503


async def test_apply_suggestion_then_preview_becomes_deterministic(client, dbSession):
    from app.tests.integration.test_dq_rule_generate_api import (
        ensureClassWithProperty, ensureDataSourceAndSchema)
    from app.domain.models import OntologyProperty
    from sqlalchemy import select
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(dbSession, tables={
        "PORDER": [("PO_KEY", "varchar", False), ("STATUS", "varchar", True)]})
    prop = (await dbSession.execute(select(OntologyProperty).where(
        OntologyProperty.class_id == classId,
        OntologyProperty.property_name == "po_key"))).scalars().one()
    res = await client.post(f"{GEN_BASE}/apply-suggestion", json={
        "propertyId": prop.id, "allowedValues": ["NEW", "CONFIRMED"]})
    assert res.status_code == 200
    preview = (await client.post(f"{GEN_BASE}/preview", json={
        "classId": classId, "datasourceId": dsId})).json()
    v = [s for s in preview["suggestions"] if s["derivationType"] == "ALLOWED_VALUES"]
    assert v and v[0]["ruleExpression"].startswith("PO_KEY IN ('NEW'")


async def test_apply_suggestion_rejects_quote_value(client, dbSession):
    ...（allowedValues 含单引号 → 422）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest app/tests/integration/test_dq_rule_generate_llm_api.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

LLM 服务（复用 `feature_rule_llm_service.py` 的结构）：加载类属性 → system prompt（逐属性列 property_name/data_type/description）→ `llm_client.complete` → json 解析 → `ParseDescriptionsResponse`；任何异常 `LLMUnavailableError`（503）。advisory 只读，不写 outbox。

`applySuggestion`：取 property（404）、校验每个 value `^[^']{1,50}$`（否则 422 `MSG_DQ_GEN_BAD_VALUE`）→ `property.allowed_values = payload.allowed_values`（ORM 局部更新）→ outbox `event_type="ontology_property_updated"`（payload 含 before/after allowed_values）→ commit → response。

路由：两个 POST，均 `getCurrentUser`；parse-descriptions 内 `llm_client = createClient(None)`（模块级 import 供 monkeypatch）。

- [ ] **Step 4: 跑测试至全绿 + 确认 token 计量**

检查 `createClient` 返回的 client 是否已内建 token 计量（查 `app/infrastructure/llm/` 中 complete 的实现，对照 `session_token_usage` 写入）。若已内建，在 LLM happy-path 测试追加断言：

```python
    from sqlalchemy import text
    usage = (await dbSession.execute(text(
        "SELECT count(*) FROM session_token_usage"))).scalar()
    assert usage >= 1
```

（fake client 不走计量时，改为断言真实链路已有单测覆盖，并在 PR 描述注明。）

Run: `cd backend && python -m pytest app/tests/integration/test_dq_rule_generate_llm_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/data_quality_rule_llm_service.py backend/app/services/data_quality_rule_generate_service.py backend/app/api/v1/data_quality_generate.py backend/app/domain/schemas.py backend/app/services/messages_zh.py backend/app/tests/integration/test_dq_rule_generate_llm_api.py
git commit -m "feat(dq): LLM 候选约束建议 + apply-suggestion 元数据沉淀"
```

---

### Task 7: 后端全量回归 + 覆盖率

**Files:**
- 无新文件（验证任务）

- [ ] **Step 1: 全量测试**

Run: `cd backend && python -m pytest --cov=app --cov-fail-under=80 -q`
Expected: PASS，覆盖率 ≥ 80%（现有 baseline ~93%，新代码应不低于 80% 增量）

- [ ] **Step 2: 用 code-reviewer / python-reviewer 审查（项目 CLAUDE.md 要求）**

Run: 派 code-reviewer agent 审查新增/修改文件；CRITICAL/HIGH 必须修复后才能继续。

- [ ] **Step 3: Commit（若有修复）**

---

### Task 8: 前端 types + api client

**Files:**
- Create: `frontend/src/types/dataQualityGenerate.ts`
- Create: `frontend/src/api/dataQualityGenerate.ts`
- Test: `frontend/src/types/__tests__/dataQualityGenerate.test.ts`（若 types 目录无测试惯例则并入 Task 9 的页面契约测试）

**Interfaces:**
- Consumes: Task 4/5/6 的 API 契约（camelCase JSON）。
- Produces（Task 9 依赖）:

```typescript
// types
export interface RuleSuggestion {
  ruleCode: string; ruleName: string; ruleType: string;
  targetTable: string; targetColumn: string | null;
  ruleExpression: string | null; threshold: number; severity: string;
  derivationType: string; sourcePropertyId: number | null;
  confidence: string; status: "NEW" | "EXISTS"; reason: string;
}
export interface GeneratePreviewResponse {
  classId: number; className: string; sourceTable: string | null;
  datasourceId: number; suggestions: RuleSuggestion[];
  blocked: { propertyName: string; reason: string }[];
}
export interface GenerateConfirmResponse {
  created: DataQualityRule[]; skippedCodes: string[];
}
// api
export async function previewRules(classId: number, datasourceId: number): Promise<GeneratePreviewResponse>
export async function confirmRules(datasourceId: number, rules: RuleSuggestion[]): Promise<GenerateConfirmResponse>
export async function parseDescriptions(classId: number): Promise<PropertyConstraintSuggestion[]>
export async function applySuggestion(propertyId: number, allowedValues: string[]): Promise<void>
```

- [ ] **Step 1: 写 types + api client**（照抄 `api/dataQuality.ts` 的 httpClient 模式：`const BASE = "/data-quality/rules/generate"`）

- [ ] **Step 2: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无错误

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types/dataQualityGenerate.ts frontend/src/api/dataQualityGenerate.ts
git commit -m "feat(dq): 生成向导 types + api client"
```

---

### Task 9: 前端向导页 + 路由 + i18n

**Files:**
- Create: `frontend/src/pages/DataQualityRuleGeneratePage.tsx`
- Modify: `frontend/src/App.tsx`（import + `<Route path="data-quality/generate" element={<DataQualityRuleGeneratePage />} />`，加在 data-quality 路由旁）
- Modify: `frontend/src/i18n/zh-CN.ts` + `frontend/src/i18n/en-US.ts`
- Test: `frontend/src/pages/__tests__/DataQualityRuleGeneratePage.contract.test.ts`（源码契约断言，参照 AgentRegistryPage 的 jsdom 契约测试模式）

**Interfaces:**
- Consumes: Task 8 的 api/types；`listClasses`（`api/ontology.ts`）、数据源列表 API（`api/datasource.ts` 现有函数，执行时读该文件确认函数名）。

页面结构（antd `Steps` 四步，组件拆分保持文件 <800 行）：

1. **Step 1**：`Select` 选本体类（`listClasses()`）；
2. **Step 2**：`Select` 选数据源 → 触发 `previewRules` → 展示 `Table`（属性名 / 物理列 / 映射状态）；blocked 属性红色标注 reason；
3. **Step 3**：建议清单 `Table`（勾选列、ruleCode、ruleType、targetColumn、ruleExpression、threshold `InputNumber`、severity `Select`、status 标签：NEW 绿 / EXISTS 灰、reason）；LLM 建议折叠面板（`parseDescriptions` 结果按属性展示，"采纳"按钮 → `applySuggestion` → 重新 preview）；
4. **Step 4**：`confirmRules` 提交 → 结果统计（created / skipped / blocked 数量）+ 成功提示。

i18n 键（两语言同步）：

```typescript
// zh-CN.ts
dataQualityGenerate: {
  title: "规则生成向导",
  stepClass: "选择本体类", stepDatasource: "选择数据源", stepPreview: "预览规则",
  stepConfirm: "确认落库", className: "本体类", datasource: "数据源",
  mappingStatus: "映射状态", suggestions: "建议规则", blocked: "未生成（被阻断）",
  llmPanel: "AI 建议（来自属性描述）", adopt: "采纳并沉淀",
  confirmBtn: "确认落库", created: "已创建", skipped: "已跳过",
  noProperties: "该类暂无属性，请先维护本体",
  previewEmpty: "请先完成前两步",
  confirmSuccess: "规则生成完成",
},
```

- [ ] **Step 1: 写页面契约测试（失败）**

```typescript
// frontend/src/pages/__tests__/DataQualityRuleGeneratePage.contract.test.ts
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

const src = readFileSync(
  new URL("../DataQualityRuleGeneratePage.tsx", import.meta.url), "utf-8");

describe("DataQualityRuleGeneratePage contract", () => {
  it("renders 4 wizard steps using i18n keys", () => {
    for (const key of ["stepClass", "stepDatasource", "stepPreview", "stepConfirm"]) {
      expect(src).toContain(`dataQualityGenerate.${key}`);
    }
  });
  it("calls confirmRules only with NEW suggestions", () => {
    expect(src).toContain('status === "NEW"');
  });
});
```

- [ ] **Step 2: 实现页面 + 路由 + i18n**

- [ ] **Step 3: 验证**

Run: `cd frontend && npx tsc --noEmit && npx vitest run src/pages/__tests__/DataQualityRuleGeneratePage.contract.test.ts`
Expected: 通过

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/DataQualityRuleGeneratePage.tsx frontend/src/App.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts frontend/src/pages/__tests__/
git commit -m "feat(dq): 规则生成向导页（四步）"
```

---

### Task 10: 菜单 seed + fallbackNav + 端到端验证

**Files:**
- Modify: `backend/scripts/seed_menu_config.py`（ITEMS 列表，约 32-61 行）
- Modify: `frontend/src/components/common/fallbackNav.ts`
- Modify: `frontend/src/i18n/zh-CN.ts` + `en-US.ts`（`menu.item.dataQualityGenerate` + `appLayout.menu.dataQualityGenerate`）

**Interfaces:**
- Produces: 菜单项 `item.dataQualityGenerate`，path `/data-quality/generate`，parent `section.bizConfig`，sort_order `325`（dataQuality=320 与 lineage=330 之间），icon_code `"thunderbolt"`。

- [ ] **Step 1: seed_menu_config.py ITEMS 追加**

```python
    {"parent": "section.bizConfig", "code": "item.dataQualityGenerate", "label_key": "menu.item.dataQualityGenerate", "icon_code": "thunderbolt", "sort_order": 325, "path": "/data-quality/generate"},
```

- [ ] **Step 2: i18n 两语言**：`menu.item.dataQualityGenerate` = "规则生成向导" / "Rule Generation Wizard"；`appLayout.menu.dataQualityGenerate` 同文案。

- [ ] **Step 3: fallbackNav.ts 追加**（紧跟 `/data-quality` 行后）：

```typescript
  { key: "/data-quality/generate", labelKey: "appLayout.menu.dataQualityGenerate" },
```

- [ ] **Step 4: 跑 seed + 菜单验证**

Run: `cd backend && python -m scripts.seed_menu_config`（或项目现有 seed 启动方式；seed 后 `docker exec qa-postgres psql` 查 `menu_config` 确认新行，参照 memory 中两 DB 区分规则先 `psql -l` 确认连的是 qa_metadata）

- [ ] **Step 5: 前端全量验证**

Run: `cd frontend && npx tsc --noEmit && npx vitest run`
Expected: 通过（既有覆盖缺口为预存问题，见 memory frontend-coverage-gate，不因本特性新增失败）

- [ ] **Step 6: Commit**

```bash
git add backend/scripts/seed_menu_config.py frontend/src/components/common/fallbackNav.ts frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts
git commit -m "feat(dq): 规则生成向导菜单入口（seed + fallback + i18n）"
```

---

### Task 11: Harness 变更记录 + 收尾

**Files:**
- Create: `Harness/changes/feat-dq-rule-auto-generation/summary.md`
- Modify: `Harness/agents/owner.md` 索引（若该文件索引变更记录）

- [ ] **Step 1: 写 change record**（参照 `Harness/changes/feat-feature-rule-config/summary.md` 结构：背景 / spec 链接 / 数据模型变更 / API / 前端 / 测试 / 遗留项——遗留项至少记录：TIMELINESS 未做、方案 B 升级路径、类型正则仅覆盖文本物理列）

- [ ] **Step 2: 后端最终全量回归**

Run: `cd backend && python -m pytest --cov=app --cov-fail-under=80 -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add Harness/changes/feat-dq-rule-auto-generation/summary.md Harness/agents/owner.md
git commit -m "docs(dq): auto-generation 变更记录"
```

---

## Self-Review 记录

- **Spec 覆盖**：§3 架构（Task 3/4）、§4 数据模型（Task 1）、§5 推导映射（Task 2/3）、§6 API 四端点（Task 4/5/6）、§7 前端+菜单（Task 9/10）、§8 错误处理（Task 3 blocked/Task 4 404/Task 5 IntegrityError/Task 6 503+422）、§9 测试（各任务内）、§10 升级路径（引擎与存储解耦在 Task 3 注释+Task 11 遗留项）。§11 不做项已遵守。
- **类型一致性**：`RuleSuggestion`/`GeneratePreviewResponse` 等签名在 Task 3/4/8 间已逐一核对；camelCase JSON 由 CamelModel / 前端 types 对齐。
- **发现并处理的坑**：表达式白名单缺 `'~^${}:`（Task 2 前置）；CONSISTENCY 评估器为单表谓词，跨表一致性用 `EXISTS (SELECT 1 FROM ... WHERE ...)` 相关子查询表达（表达式字符在现有白名单内）；ref 指向 Reference 类时只生成 DICT_REF VALIDITY、不再叠加 REFERENTIAL（避免重复规则）。
