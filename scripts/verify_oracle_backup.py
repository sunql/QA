#!/usr/bin/env python3
"""校验 Oracle 备份的**完整性**：每张表「导出的 INSERT 行数」 vs「库里的真实行数」。

口径与 scripts/backup_oracle_schema.py 的导出循环**完全一致**（复用它的
`_pickFilterColumn` / `_filterClause` / `_detectYmKind` / `_detectTextDateKind`），
因此能回答「这张表有没有被漏导 / 导残」。

- 导出用**时间过滤**的表 ⇒ 库里按同一条件 COUNT(*)；
- 导出用**全量**的表 ⇒ 库里 COUNT(*)；
- 不一致 ⇒ 该表**导残了**，必须重导。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))

import backup_oracle_schema as bos  # noqa: E402
import oracledb  # noqa: E402

INSERT_RE = re.compile(r'^INSERT INTO "(\w+)"')


def exportedCounts(sqlPath: pathlib.Path) -> dict[str, int]:
    """从合并后的 SQL 里数每张表的 INSERT 行数（流式，不整file读进内存）。"""
    counts: dict[str, int] = {}
    with sqlPath.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith('INSERT INTO "'):
                name = line.split('"')[1]
                counts[name] = counts.get(name, 0) + 1
    return counts


async def main() -> int:
    parser = argparse.ArgumentParser(description="校验备份完整性")
    parser.add_argument("--sql", required=True, help="合并后的备份 SQL")
    parser.add_argument("--out", required=True, help="输出校验报告 JSON")
    parser.add_argument("--from-year", type=int, default=2025)
    parser.add_argument("--datasource-id", type=int, default=1)
    args = parser.parse_args()

    from sqlalchemy import select

    from app.domain.models import DataSource
    from app.infrastructure.database import getSessionFactory
    from app.infrastructure.security.crypto import decryptApiKey

    factory = getSessionFactory()
    async with factory() as session:
        ds = (
            await session.execute(
                select(DataSource).where(DataSource.id == args.datasource_id)
            )
        ).scalars().first()
    password = decryptApiKey(ds.password_encrypted)
    dsn = f"{ds.host}:{ds.port}/{ds.database_name}"
    connection = await oracledb.connect_async(user=ds.username, password=password, dsn=dsn)
    cursor = connection.cursor()

    owner = "THBI"
    tables = await bos._columnsOf(cursor, owner)
    counts = exportedCounts(pathlib.Path(args.sql))

    report: list[dict] = []
    mismatches = 0
    for table in tables:
        name = table["name"]
        exported = counts.get(name)
        filterColumn, filterKind = bos._pickFilterColumn(name, table["columns"])
        where = ""
        if filterColumn is not None:
            if filterKind == "ym":
                filterKind = await bos._detectYmKind(cursor, owner, name, filterColumn)
            elif filterKind == "text-date":
                filterKind = await bos._detectTextDateKind(
                    cursor, owner, name, filterColumn
                )
            if filterKind in ("ym-unknown", "text-unknown"):
                filterColumn, filterKind = None, "none"
            else:
                where = " WHERE " + bos._filterClause(
                    filterColumn, filterKind, args.from_year
                )
        sql = f'SELECT COUNT(*) FROM "{owner}"."{name}"{where}'
        await cursor.execute(sql)
        actual = int((await cursor.fetchone())[0])
        fallback = False
        if actual == 0 and where:
            # ⚠️ 导出脚本的兜底：按时间过滤后 **0 行** ⇒ 自动**回退为全量导出**
            # （EBS 的 'VALID_FROM'/'LIFSTRDAT_0' 这类字符串日期列就是这种情况）。
            # ⇒ 校验必须复现这条规则，否则会把「正确的全量导出」误判成不一致。
            fallback = True
            await cursor.execute(f'SELECT COUNT(*) FROM "{owner}"."{name}"')
            actual = int((await cursor.fetchone())[0])
        if exported is None:
            # 导出文件里没有这表的 INSERT ⇒ 要么表是**真空的**，要么这表**漏导了**
            status = "OK" if actual == 0 else "MISSING"
        else:
            status = "OK" if actual == exported else "MISMATCH"
        if status != "OK":
            mismatches += 1
        report.append(
            {
                "table": name,
                "status": status,
                "exported": exported,
                "actual": actual,
                "filterColumn": filterColumn,
                "filterKind": filterKind,
                "fallbackFullExport": fallback,
            }
        )
        flag = "✔" if status == "OK" else "✘"
        exportedText = f"{exported:>10,}" if exported is not None else "       (空表)"
        print(
            f"{flag} {name:42s} 导出={exportedText}  库={actual:>10,}  "
            f"过滤={filterColumn or '（全量）'}"
            f"{'  [回退全量]' if fallback else ''}",
            flush=True,
        )
    cursor.close()
    await connection.close()

    pathlib.Path(args.out).write_text(
        json.dumps(
            {
                "schema": owner,
                "fromYear": args.from_year,
                "tableCount": len(report),
                "mismatchCount": mismatches,
                "tables": report,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n[verify] 表={len(report)} 不一致={mismatches} → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
