"""`data_source` 裸表名 → 数据源的精确 join（真实 PG + 完整 API 链路）。

覆盖：
- 单表 / 多表（'+' 与空格两种分隔，含**无空格**的 ``A+B``）都能定位到数据源
- 表名大小写不敏感
- 停用数据源被排除（正向对照）
- 未命中表名 ⇒ 无该信号（正向对照）
- 与 legacy 名称信号并存时两条 reasons 合并，且 reasons 按**分值降序**排列
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, KpiCatalog, SchemaCache

ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}


async def _addSource(
    dbSession: AsyncSession, name: str, *, isActive: bool = True
) -> DataSource:
    ds = DataSource(
        name=name,
        type="mysql",
        host="x",
        port=3306,
        database_name="x",
        username="x",
        password_encrypted="x",
        is_active=isActive,
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def _addSchema(
    dbSession: AsyncSession, datasourceId: int, tables: list[str]
) -> None:
    dbSession.add(
        SchemaCache(
            datasource_id=datasourceId,
            schema_name="",
            schema_data=[{"table_name": t, "columns": []} for t in tables],
            schema_version="md5-table-join",
        )
    )
    await dbSession.commit()


async def _addKpi(dbSession: AsyncSession, code: str, dataSource: str | None) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=code,
        version="1",
        semantic_keywords=[],
        data_source=dataSource,
        metric_id=None,
        datasource_id=None,
    )
    dbSession.add(kpi)
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return kpi


async def _suggestions(client: AsyncClient, kpiId: int) -> list[dict]:
    resp = await client.get(
        f"/api/v1/kpi-catalog/{kpiId}/datasource-suggestions", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_single_bare_table_name_suggests_datasource(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    ds = await _addSource(dbSession, "TABLE_JOIN_DS")
    await _addSchema(dbSession, ds.id, ["DWD_PAYMENT"])
    kpi = await _addKpi(dbSession, "TJ_KPI_SINGLE", "DWD_PAYMENT")

    rows = await _suggestions(client, kpi.id)

    assert rows, "裸表名应能建议出数据源"
    assert rows[0]["datasourceId"] == ds.id
    assert "legacy_table_exact" in rows[0]["reasons"]


@pytest.mark.asyncio
async def test_plus_separated_tables_suggest_datasource(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    ds = await _addSource(dbSession, "TABLE_JOIN_DS2")
    await _addSchema(dbSession, ds.id, ["DWD_PURCHASE_ORDER", "DWD_GOODS_RECEIPT"])
    kpi = await _addKpi(
        dbSession, "TJ_KPI_MULTI", "DWD_PURCHASE_ORDER + DWD_GOODS_RECEIPT"
    )

    rows = await _suggestions(client, kpi.id)

    assert rows[0]["datasourceId"] == ds.id
    assert "legacy_table_exact" in rows[0]["reasons"]


@pytest.mark.asyncio
async def test_space_separated_tables_suggest_datasource(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    ds = await _addSource(dbSession, "TABLE_JOIN_DS3")
    await _addSchema(dbSession, ds.id, ["T_A", "T_B"])
    kpi = await _addKpi(dbSession, "TJ_KPI_SPACE", "T_A T_B")

    rows = await _suggestions(client, kpi.id)

    assert rows[0]["datasourceId"] == ds.id
    assert "legacy_table_exact" in rows[0]["reasons"]


@pytest.mark.asyncio
async def test_table_name_is_case_insensitive(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    ds = await _addSource(dbSession, "TABLE_JOIN_DS4")
    await _addSchema(dbSession, ds.id, ["DWD_PAYMENT"])
    kpi = await _addKpi(dbSession, "TJ_KPI_CASE", "dwd_payment")

    rows = await _suggestions(client, kpi.id)

    assert rows[0]["datasourceId"] == ds.id


@pytest.mark.asyncio
async def test_inactive_datasource_is_excluded(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """停用数据源即便持有同名表，也不进建议（正向对照：同名表的启用源在列表里）。"""
    active = await _addSource(dbSession, "TABLE_JOIN_ACTIVE")
    await _addSchema(dbSession, active.id, ["DWD_PAYMENT"])
    inactive = await _addSource(dbSession, "TABLE_JOIN_INACTIVE", isActive=False)
    await _addSchema(dbSession, inactive.id, ["DWD_PAYMENT"])
    kpi = await _addKpi(dbSession, "TJ_KPI_INACTIVE", "DWD_PAYMENT")

    rows = await _suggestions(client, kpi.id)

    assert [r["datasourceId"] for r in rows] == [active.id]
    assert all(r["datasourceId"] != inactive.id for r in rows)


@pytest.mark.asyncio
async def test_unknown_table_name_yields_no_table_signal(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """未命中的表名不产生该信号（正向对照：同一 fixture 下命中表名的 KPI 有信号）。"""
    ds = await _addSource(dbSession, "TABLE_JOIN_DS5")
    await _addSchema(dbSession, ds.id, ["DWD_PAYMENT"])
    missKpi = await _addKpi(dbSession, "TJ_KPI_NOHIT", "DWD_NOT_EXIST")
    hitKpi = await _addKpi(dbSession, "TJ_KPI_HIT", "DWD_PAYMENT")

    missRows = await _suggestions(client, missKpi.id)
    hitRows = await _suggestions(client, hitKpi.id)

    assert missRows == []
    assert [r["datasourceId"] for r in hitRows] == [ds.id]
    assert "legacy_table_exact" in hitRows[0]["reasons"]


@pytest.mark.asyncio
async def test_plus_without_spaces_suggests_all_tables(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """存量真实形态可能是 ``"A+B"``（**无空格**）⇒ 两个表名都必须被拆开并识别。

    对照：若分隔符正则漏掉 '+'，整串会变成单个 token ⇒ 一条建议都拿不到。
    """
    first = await _addSource(dbSession, "TABLE_JOIN_NOSP_A")
    await _addSchema(dbSession, first.id, ["DWD_PURCHASE_ORDER"])
    second = await _addSource(dbSession, "TABLE_JOIN_NOSP_B")
    await _addSchema(dbSession, second.id, ["DWD_GOODS_RECEIPT"])
    kpi = await _addKpi(
        dbSession, "TJ_KPI_NOSPACE_PLUS", "DWD_PURCHASE_ORDER+DWD_GOODS_RECEIPT"
    )

    rows = await _suggestions(client, kpi.id)

    assert rows, "无空格 '+' 分隔的裸表名应能建议出数据源"
    assert "legacy_table_exact" in rows[0]["reasons"]
    assert {r["datasourceId"] for r in rows} == {first.id, second.id}
    assert all("legacy_table_exact" in r["reasons"] for r in rows)


@pytest.mark.asyncio
async def test_reasons_are_sorted_by_score_descending(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同一数据源同时被 fuzzy(0.55) 与裸表名精确(0.85) 命中 ⇒ reasons 按分值降序。

    前端只渲染 ``reasons[0]`` ⇒ 若 reasons 是命中顺序，用户会看到「部分匹配」
    文案，而实际最强信号是**表名精确命中**（标签与置信度自相矛盾）。
    """
    ds = await _addSource(dbSession, "THBI Oracle")
    await _addSchema(dbSession, ds.id, ["ORDERS"])
    kpi = await _addKpi(dbSession, "TJ_KPI_REASON_ORDER", "THBI Oracle ORDERS")

    rows = await _suggestions(client, kpi.id)

    assert len(rows) == 1
    assert rows[0]["datasourceId"] == ds.id
    assert rows[0]["score"] == pytest.approx(0.85)
    assert rows[0]["reasons"] == ["legacy_table_exact", "legacy_name_fuzzy"]


@pytest.mark.asyncio
async def test_datasource_name_fragment_does_not_leak_into_other_datasource(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """B-3：含空白数据源名被切碎后，碎片不能冒充别的数据源的表名。

    正确源 ``THBI Oracle`` 持有表 ``ORDERS``；错误源 ``AAA WRONG`` 恰好持有表
    ``THBI``（片段碰名）。``data_source = "THBI Oracle ORDERS"`` 未修复时会被
    切成 ``THBI/ORACLE/ORDERS`` ⇒ ``THBI`` 给 ``AAA WRONG`` 产出 0.85 的
    ``legacy_table_exact``。

    错误源名必须以 ``AAA`` 开头：建议按 ``(-score, name)`` 排序，若两个源都是
    0.85，字典序会让 ``THBI Oracle`` 天然排第一 ⇒ 「正确源在 TOP」断言恒真。
    改成 ``AAA WRONG`` 后，只有「0.85 压过 0.80」真的生效，正确源才可能在首位。

    必须**双向**断言：只断言正确源存在会假绿（错源仍可能并列出现）。
    """
    correct = await _addSource(dbSession, "THBI Oracle")
    await _addSchema(dbSession, correct.id, ["ORDERS"])
    wrong = await _addSource(dbSession, "AAA WRONG")
    await _addSchema(dbSession, wrong.id, ["THBI"])
    kpi = await _addKpi(dbSession, "TJ_KPI_DS_FRAGMENT", "THBI Oracle ORDERS")

    rows = await _suggestions(client, kpi.id)

    # 正向：正确源仍在 TOP，且表名信号没被整体摘没
    assert rows[0]["datasourceId"] == correct.id
    assert rows[0]["score"] == pytest.approx(0.85)
    assert "legacy_table_exact" in rows[0]["reasons"]
    # 反向：碎片碰撞的错源不得出现在建议里
    assert all(r["datasourceId"] != wrong.id for r in rows)


@pytest.mark.asyncio
async def test_legacy_name_and_table_signals_merge(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同一数据源既被 legacy 名称精确命中、又被裸表名精确命中 ⇒ reasons 合并、score 取高。"""
    ds = await _addSource(dbSession, "DWD_PAYMENT")
    await _addSchema(dbSession, ds.id, ["DWD_PAYMENT"])
    kpi = await _addKpi(dbSession, "TJ_KPI_MERGE", "DWD_PAYMENT")

    rows = await _suggestions(client, kpi.id)

    assert len(rows) == 1
    assert rows[0]["datasourceId"] == ds.id
    assert rows[0]["score"] == pytest.approx(0.90)
    assert rows[0]["reasons"] == ["legacy_name_exact", "legacy_table_exact"]
