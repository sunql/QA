"""只读探针：物料「品类 / 外购」属性能不能支撑按类目查供应商。

回答一个问题：用户问「外购标准件的供应商情况」时，业务库里到底有没有
可用的分类信号？没有的话，工具层加 category 参数也是白加。

6 组只读查询（全部 SELECT，不写任何数据）：

    P0  物料总量
    P1  TSICOD_0..4（X3 统计组/分类码）取值分布
    P2  PURFLG_0 / MFGFLG_0（外购/自制标志）分布
    P3  名称含关键词（默认「标准件」）的物料 + 样例
    P4  名称含「外购」的物料 + 样例
    P5  若 P3 有命中：这些物料落在哪些 TSICOD_0（判断是否已有现成分类码）
    P6  尝试读分类字典表（X3 统计组定义），读不到属正常

用法（`backend/` 目录下）：

    python -m scripts.probe_material_category
    python -m scripts.probe_material_category --datasource-id 1 --top 20
    python -m scripts.probe_material_category --keyword 紧固件

适配器注意：`execute_read_only(sql)` **只收 SQL 文本**（无绑定参数），
且返回字典的键是**小写**（实测 `SELECT x AS V` → `{"v": …}`）；
X3 外源偶发 `DPY-4011 connection closed`，故单条查询失败会重试一次。

依据：2026-11-23 讨论「为什么必须给物料编码/描述，不能按品类查」——
本地元数据里 ODS_ITMMASTER 有 TSICOD_0..4 / PURFLG_0，但 248 个属性
0 个中文别名，且 material_search 只查 entity_mapping（无分类列）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        key, _, val = line.strip().partition("=")
        os.environ[key] = val.strip()

from sqlalchemy import select  # noqa: E402

from app.domain.models import DataSource  # noqa: E402
from app.infrastructure.business_db_pool import get_adapter  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402

TABLE = "ZJTH.ITMMASTER"
STAT_COLUMNS = ("TSICOD_0", "TSICOD_1", "TSICOD_2", "TSICOD_3", "TSICOD_4")
FLAG_COLUMNS = ("PURFLG_0", "MFGFLG_0")
RETRY_DELAY_SECONDS = 1.5


def _section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _get(row: dict[str, Any], name: str, default: Any = None) -> Any:
    """大小写不敏感取值（适配器返回小写键，SQL 里写的是大写）。"""
    if name in row:
        return row[name]
    lowered = name.lower()
    if lowered in row:
        return row[lowered]
    return row.get(name.upper(), default)


def _shown(value: Any) -> str:
    return "<NULL/空白>" if value in (None, "") or str(value).strip() == "" else str(value)


async def _try_query(adapter, sql: str, label: str = "") -> list[dict]:
    """执行只读查询；X3 外源偶发断连时重试一次，仍失败只报告不中断。"""
    for attempt in (1, 2):
        try:
            rows = await adapter.execute_read_only(sql)
            if label:
                print(f"  {label}: {len(rows)} 行")
            return rows
        except Exception as exc:  # noqa: BLE001 — 探针：任何错误都记录后继续
            if attempt == 1:
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue
            print(f"  !! 查询失败（{label or sql[:40]}）：{type(exc).__name__}: {exc}")
    return []


def _likeLiteral(keyword: str) -> str:
    """关键词 → 行内 LIKE 字面量（无绑定参数，手工转义单引号）。"""
    return "'%" + keyword.replace("'", "''") + "%'"


def _distinctSql(column: str, top: int) -> str:
    return (
        f"SELECT {column} AS V, COUNT(*) AS CNT FROM {TABLE} "
        f"GROUP BY {column} ORDER BY CNT DESC FETCH FIRST {top} ROWS ONLY"
    )


async def _probeStatGroups(adapter, top: int) -> None:
    _section(f"P1 统计组/分类码 TSICOD_0..4 取值分布（每列前 {top}）")
    for column in STAT_COLUMNS:
        rows = await _try_query(adapter, _distinctSql(column, top), label=column)
        if not rows:
            continue
        blank = sum(c for r in rows if (_get(r, "V") is None or str(_get(r, "V")).strip() == "") for c in [_get(r, "CNT", 0)])
        print(f"    其中空值/空白合计（前 {top} 内）: {blank}")
        for row in rows[:top]:
            print(f"    {_shown(_get(row, 'V')):<22} {_get(row, 'CNT')}")


async def _probeFlags(adapter) -> None:
    _section("P2 外购/自制标志分布")
    for column in FLAG_COLUMNS:
        rows = await _try_query(adapter, _distinctSql(column, 10), label=column)
        for row in rows:
            print(f"    {column} = {_shown(_get(row, 'V')):<12} {_get(row, 'CNT')}")


async def _probeKeyword(adapter, keyword: str, tag: str) -> int:
    _section(f"{tag} 名称含「{keyword}」的物料")
    literal = _likeLiteral(keyword)
    rows = await _try_query(adapter, f"SELECT COUNT(*) AS CNT FROM {TABLE} WHERE ITMDES1_0 LIKE {literal}", label="count")
    total = int(_get(rows[0], "CNT", 0)) if rows else 0
    print(f"  命中总数: {total}")
    if not total:
        return 0
    sample = await _try_query(
        adapter,
        f"SELECT ITMREF_0, ITMDES1_0, TSICOD_0, TSICOD_1, PURFLG_0 "
        f"FROM {TABLE} WHERE ITMDES1_0 LIKE {literal} "
        f"ORDER BY ITMREF_0 FETCH FIRST 10 ROWS ONLY",
        label="sample",
    )
    for row in sample:
        print(
            f"    {_get(row, 'ITMREF_0')} | {_get(row, 'ITMDES1_0')} | "
            f"TSICOD_0={_shown(_get(row, 'TSICOD_0'))} "
            f"TSICOD_1={_shown(_get(row, 'TSICOD_1'))} "
            f"PURFLG_0={_shown(_get(row, 'PURFLG_0'))}"
        )
    return total


async def _probeCategoryOfKeyword(adapter, keyword: str) -> None:
    _section(f"P5 「{keyword}」物料落在哪些 TSICOD_0（判断是否已有现成分类码）")
    sql = (
        f"SELECT TSICOD_0 AS V, COUNT(*) AS CNT FROM {TABLE} "
        f"WHERE ITMDES1_0 LIKE {_likeLiteral(keyword)} GROUP BY TSICOD_0 "
        f"ORDER BY CNT DESC FETCH FIRST 15 ROWS ONLY"
    )
    rows = await _try_query(adapter, sql, label="tsicod")
    for row in rows:
        print(f"    TSICOD_0 = {_shown(_get(row, 'V')):<22} {_get(row, 'CNT')}")


async def _dumpCategories(adapter) -> None:
    """导出 ITMCATEG 品类码（TCLCOD_0 → TSICOD_0）；业务据此标注中文名。"""
    _section("P7 ITMCATEG 品类码清单（业务标注中文名后，才能把「标准件」映射成码）")
    rows = await _try_query(
        adapter,
        "SELECT TCLCOD_0, TSICOD_0, ACCCOD_0, TCLDES_0 FROM ZJTH.ITMCATEG "
        "ORDER BY TCLCOD_0",
        label="ITMCATEG",
    )
    for row in rows:
        print(
            f"    TCLCOD={_get(row, 'TCLCOD_0')!s:<6} "
            f"TSICOD_0={_shown(_get(row, 'TSICOD_0')):<10} "
            f"ACCCNT={_get(row, 'ACCCOD_0')!s:<6} "
            f"描述={_shown(_get(row, 'TCLDES_0'))}"
        )
    print(
        "\n    说明：TCLDES_0（品类描述）在库里**全为空**，所以这份清单只有码、"
        "没有名字 —— 必须由业务补中文名，否则工具层加了参数也没法把"
        "「标准件」落到某个码上。"
    )


async def _probeDictionary(adapter) -> None:
    _section("P6 分类字典表探测（X3 统计组定义；读不到属正常）")
    for candidate in ("ZJTH.TSICOD", "ZJTH.ATABDIV"):
        rows = await _try_query(adapter, f"SELECT * FROM {candidate} FETCH FIRST 10 ROWS ONLY", label=candidate)
        if rows:
            print(f"  {candidate} 列: {list(rows[0].keys())}")
            for row in rows[:5]:
                print(f"    {row}")


async def _pickDatasource(session, datasourceId: int | None) -> DataSource:
    if datasourceId is not None:
        return (
            await session.execute(select(DataSource).where(DataSource.id == datasourceId))
        ).scalar_one()
    stmt = (
        select(DataSource)
        .where(DataSource.is_active.is_(True))
        .where(DataSource.type.in_(("oracle", "ORACLE")))
        .order_by(DataSource.id)
    )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        raise SystemExit("没有 active 的 Oracle 数据源，请显式 --datasource-id")
    return rows[0]


async def main() -> None:
    parser = argparse.ArgumentParser(description="物料品类/外购属性只读探针")
    parser.add_argument("--datasource-id", type=int, default=None)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--keyword", default="标准件", help="品类关键词（默认 标准件）")
    parser.add_argument(
        "--dump-categories",
        action="store_true",
        help="导出 ITMCATEG 品类码清单（给业务标注中文名用），并跳过关键词段",
    )
    args = parser.parse_args()

    async with getSessionFactory()() as session:
        ds = await _pickDatasource(session, args.datasource_id)

    print(f"数据源: #{ds.id} {ds.name} ({ds.type} @ {ds.host}/{ds.database_name})")
    adapter = get_adapter(ds.id, ds)

    _section("P0 物料总量")
    rows = await _try_query(adapter, f"SELECT COUNT(*) AS CNT FROM {TABLE}", label="ITMMASTER")
    if rows:
        print(f"  物料总数: {_get(rows[0], 'CNT')}")

    await _probeStatGroups(adapter, args.top)
    await _probeFlags(adapter)
    hits = 0
    if not args.dump_categories:
        hits = await _probeKeyword(adapter, args.keyword, "P3")
        await _probeKeyword(adapter, "外购", "P4")
        if hits:
            await _probeCategoryOfKeyword(adapter, args.keyword)
    await _dumpCategories(adapter)
    await _probeDictionary(adapter)

    _section("怎么看这份输出")
    print(
        "  1) P5 若有明显主码（某个 TSICOD_0 覆盖绝大多数命中）⇒ 品类可映射成分类码，\n"
        "     走「②语义层补别名 + ③material_search 加 statGroup 参数」；\n"
        "  2) P5 全空或极分散 ⇒ ERP 里没有「标准件」这个分类，需业务先建品类或走术语表；\n"
        "  3) P3 命中 0 而 P4 有值 ⇒ 关键词层面就答不了，优先做同义词/别名治理，而不是加参数。"
    )


if __name__ == "__main__":
    if sys.platform == "darwin":
        pass
    asyncio.run(main())
