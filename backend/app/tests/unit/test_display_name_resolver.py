"""显示名解析服务：列名/值 → 中文标签。

真实 PG 测试：测试 OntologyProperty 命中 + 缺失 + value dict 命中 + fallback。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass, OntologyProperty
from app.services.display_name_resolver import (
    DISPLAY_COLUMN_DICT,  # noqa: F401  字典本体：R4 指定的导入面，断言经 resolveColumnName 间接完成
    DISPLAY_VALUE_DICT,  # noqa: F401  同上（本行在 R4 前即未用）
    deriveSourceTable,
    resolveColumnName,
    resolveValueName,
)


@pytest.mark.asyncio
async def test_resolveColumnName_returns_alias_when_found(db_session: AsyncSession) -> None:
    """seed OntologyClass(source_table=foo) + OntologyProperty(source_column=bar, alias='基地代码')
    → resolveColumnName returns '基地代码'.

    注：实际 ORM 模型里 `source_table` 在 `OntologyClass` 上，`OntologyProperty` 通过
    `class_id` 外键引用类。seed 时先落 class 再落 property。
    """
    cls = OntologyClass(class_name="foo", source_table="foo")
    db_session.add(cls)
    await db_session.flush()

    db_session.add(
        OntologyProperty(
            class_id=cls.id,
            property_name="bar",
            data_type="string",
            source_column="bar",
            property_alias="基地代码",
        )
    )
    await db_session.commit()

    result = await resolveColumnName(db_session, "foo", "bar")
    assert result == "基地代码"


@pytest.mark.asyncio
async def test_resolveColumnName_falls_back_to_column_name(db_session: AsyncSession) -> None:
    """无 property 行 → 返原 column 名."""
    result = await resolveColumnName(db_session, "foo", "unknown_col")
    assert result == "unknown_col"


def test_resolveValueName_returns_dict_value() -> None:
    """DISPLAY_VALUE_DICT 命中：'QTY' → '数量确认'."""
    result = resolveValueName("dwd_reconciliation_bill_dtl", "confirm_stage", "QTY")
    assert result == "数量确认"


def test_resolveValueName_falls_back_to_str() -> None:
    """未命中 + None + 缺 sourceTable/Column 全部 fallback."""
    # 未命中：返原 value 字符串
    assert resolveValueName("dwd_reconciliation_bill_dtl", "site_code", "C1") == "C1"
    # None → "—"
    assert resolveValueName(None, None, None) == "—"
    # 缺 sourceTable → 返原 value
    assert resolveValueName(None, "confirm_stage", "QTY") == "QTY"
    # 缺 sourceColumn → 返原 value
    assert resolveValueName("dwd_reconciliation_bill_dtl", None, "QTY") == "QTY"


# --- R4 Task 2：从支撑 SQL 派生来源表 ----------------------------------------


def test_deriveSourceTable_reads_first_from() -> None:
    """最简 FROM → 表名大写返回."""
    sql = "SELECT SITE_CODE, COUNT(*) AS bill_cnt FROM DWD_RECONCILIATION_BILL_DTL GROUP BY SITE_CODE"
    assert deriveSourceTable(sql) == "DWD_RECONCILIATION_BILL_DTL"


def test_deriveSourceTable_strips_schema_prefix_and_quotes() -> None:
    """schema 前缀 + 双引号（Oracle/ANSI 引号标识符）都要剥掉."""
    assert deriveSourceTable('SELECT a FROM "DW"."DWD_X"') == "DWD_X"
    assert deriveSourceTable("select a from dw.dwd_x") == "DWD_X"


def test_deriveSourceTable_reads_inside_cte_body() -> None:
    """WITH 里的 FROM 出现在外层 FROM 之前 ⇒ 取到的是**真实业务表**，不是 CTE 名."""
    sql = "WITH monthly AS (SELECT PERIOD_MONTH, SUM(AMT) FROM DWD_RECONCILIATION_BILL_DTL GROUP BY 1) SELECT * FROM monthly"
    assert deriveSourceTable(sql) == "DWD_RECONCILIATION_BILL_DTL"


def test_deriveSourceTable_returns_none_without_from() -> None:
    """无 FROM / 空串 / None 一律 None（翻译整体降级为原样）."""
    assert deriveSourceTable("SELECT 1") is None
    assert deriveSourceTable("") is None
    assert deriveSourceTable(None) is None


# --- R4 Task 2：代码内列名字典 + 大小写归一 -----------------------------------


@pytest.mark.asyncio
async def test_resolveColumnName_uses_code_dict_when_ontology_has_no_alias(
    db_session: AsyncSession,
) -> None:
    """本体无 alias（真实库常态：3699 行里仅 3 行有）⇒ 落到代码内列名字典."""
    # 该表在测试库无 OntologyClass 行 ⇒ 本体层必然落空
    assert (
        await resolveColumnName(db_session, "DWD_RECONCILIATION_BILL_DTL", "SITE_CODE")
        == "基地"
    )


@pytest.mark.asyncio
async def test_resolveColumnName_normalizes_case(db_session: AsyncSession) -> None:
    """行数据键是小写、本体列是大写 ⇒ 大小写归一后仍命中字典."""
    assert (
        await resolveColumnName(db_session, "dwd_reconciliation_bill_dtl", "site_code")
        == "基地"
    )


@pytest.mark.asyncio
async def test_resolveColumnName_ontology_alias_beats_code_dict(
    db_session: AsyncSession,
) -> None:
    """本体 alias 是主层：有值时**压过**代码内字典."""
    cls = OntologyClass(class_name="dwd_x", source_table="dwd_x")
    db_session.add(cls)
    await db_session.flush()
    db_session.add(
        OntologyProperty(
            class_id=cls.id,
            property_name="site_code",
            data_type="string",
            source_column="SITE_CODE",
            property_alias="本体基地名",
        )
    )
    db_session.add(
        OntologyProperty(
            class_id=cls.id,
            property_name="site_code_dup",
            data_type="string",
            source_column="SITE_CODE",
            property_alias="本体基地名",
        )
    )
    await db_session.commit()
    assert await resolveColumnName(db_session, "DWD_X", "site_code") == "本体基地名"


@pytest.mark.asyncio
async def test_resolveColumnName_works_without_db() -> None:
    """db=None（旧夹具直接调 / 降级路径）⇒ 只走代码内字典，不抛."""
    assert await resolveColumnName(None, "DWD_RECONCILIATION_BILL_DTL", "SITE_CODE") == "基地"
    assert await resolveColumnName(None, "DWD_RECONCILIATION_BILL_DTL", "NO_SUCH") == "NO_SUCH"


def test_resolveValueName_normalizes_table_case() -> None:
    """值字典查表前归一为大写（R3 的 dict 键原是小写 ⇒ F4 的 bug）."""
    assert resolveValueName("DWD_RECONCILIATION_BILL_DTL", "CONFIRM_STAGE", "QTY") == "数量确认"
    assert resolveValueName("dwd_reconciliation_bill_dtl", "confirm_stage", "QTY") == "数量确认"