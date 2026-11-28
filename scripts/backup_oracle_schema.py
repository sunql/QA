#!/usr/bin/env python3
"""scripts/backup_oracle_schema.py —— Oracle **schema 级**备份（无 Oracle 客户端依赖）。

为什么需要这个（2026-11-27）：
- 本机**没有** expdp / sqlplus（Oracle 客户端工具不在环境里）；
- 但后端 venv 里有 `oracledb`（纯 Python thin 驱动），且开发机**能连**业务库；
- `expdp` 还需要 DB 上先建 DIRECTORY 对象（要 DBA 权限），纯驱动方案不需要。

产出（默认 `backups/oracle/`）：
- `<schema>_schema_YYYYMMDD_HHMM.sql`  —— 可直接 `sqlplus @` 回放的 SQL 脚本
  （CREATE TABLE + INSERT，**含 LOB 的表按需降级**，见下）
- `<schema>_schema_YYYYMMDD_HHMM.manifest.json` —— 表清单 / 行数 / 列数 / 字节量 / 跳过原因

⚠️ **LOB 列的处理**：Oracle 的 CLOB/BLOB 不能直接拼进 INSERT 字面量（超 4000 字符、
二进制转义脆弱）⇒ 这类列**导出为十六进制 + DBMS_LOB.SUBSTR/TO_BLOB 的分块写法**代价高且易错。
本脚本的取舍：**LOB 列不导数据，只导结构 + 其余列的数据**，并在 manifest 里逐表列出
被跳过的 LOB 列清单 ⇒ 恢复后 LOB 字段为空（结构在）。要连 LOB 数据一起备，走
`expdp`（需 DBA 建 DIRECTORY）。

⚠️ **只读**：全程只发 SELECT，不写业务库。

用法（在开发机 / 任何能连业务库的机器）：
    cd backend && uv run python ../scripts/backup_oracle_schema.py \
        --datasource-id 1 --schema THBI --out ../backups/oracle
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if BACKEND.exists():
    sys.path.insert(0, str(BACKEND))

import oracledb  # noqa: E402

#: 不可直接导数据的列类型（LOB / ROWID / LONG）。
_SKIP_COLUMN_TYPES = frozenset(
    {
        "CLOB", "NCLOB", "BLOB", "BFILE", "LONG", "LONG RAW", "ROWID",
        "XMLTYPE", "ANYDATA", "SDO_GEOMETRY",
    }
)


def _quote(name: str) -> str:
    """Oracle 标识符加引号（大小写敏感时按库里的原样）。"""
    return '"' + name.replace('"', '""') + '"'


def _literal(value: Any) -> str:
    """Python 值 → Oracle 字面量（None → NULL）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return f"TO_DATE('{value.strftime('%Y-%m-%d %H:%M:%S')}', 'YYYY-MM-DD HH24:MI:SS')"
    if isinstance(value, bytes):
        return f"HEXTORAW('{value.hex()}')"
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


#: **真业务日期列**（订单/到货/发票等**业务时点**，不是入库时间）。
#: 命中即用；否则退到「其它非技术性的 DATE/TIMESTAMP 列」；再退到 YMONTH（按实际格式）。
#:
#: ⚠️ **CREATION_DATE 一类「入库/ETL 时间戳」不在此列** —— 拿它做 `>= 2025` 过滤会
#: 把**主数据/存量数据**整片滤掉（实测 DIM_IMATERIAL 350,922 行 → 导出 0 行；
#: DIM_SUPPLIER 3,500 → 0；DWD_ITEM_FACILITY 818,347 → 231,402）⇒ 备份不可用。
_BUSINESS_DATE_COLUMNS = (
    "ORDER_DATE", "EXP_RCV_DATE", "EXPECTED_DATE", "EXPECT_RECEIPT_DATE",
    "RECEIPT_DATE", "ACTUAL_RECEIPT_DATE", "ARR_DATE", "ARRIVAL_DATE",
    "DELIVERY_DATE", "INVOICE_DATE", "BILL_DATE", "DOC_DATE", "PLAN_DATE",
    "RTN_DATE", "APPLY_DATE", "VALID_STR_DATE",
)

#: **技术时间戳**（入库 / ETL 批次 / 抽取时间）—— **不能**当业务日期用。
_TECHNICAL_DATE_COLUMNS = (
    "CREATION_DATE", "CREATED_DATE", "CREATION_TIME", "CREATED_TIME",
    "ETL_LOAD_TS", "ETL_LOAD_TIME", "UPDATED_DATE", "UPDATE_DATE",
    "LAST_MODIFIED_DATE", "LOAD_DATE", "LOAD_TIME", "BATCH_DATE",
    "EXTRACT_TIME", "EXTRACT_DATE",
)

#: 维度表前缀 —— **一律全量导出**（维度表是主数据，按时间切会丢历史版本）。
_DIMENSION_TABLE_PREFIXES = ("DIM_",)


def _pickFilterColumn(
    tableName: str, columns: list[dict[str, Any]]
) -> tuple[str | None, str]:
    """挑「用哪一列做时间过滤」⇒ (列名, 过滤表达式类型)。

    返回列名 None ⇒ 该表**不做时间过滤** ⇒ 导全量（在 manifest 里标注）。
    类型：
    - ``date``：DATE/TIMESTAMP 列 ⇒ 与 ``DATE '2025-01-01'`` 比较；
    - ``text-date``：**VARCHAR** 存 ISO 日期（'2025-01-01'）⇒ 字符串比较；
    - ``ym``：``YMONTH`` 列 ⇒ 按实测格式拼 '2025-01' / '202501'；
    - ``none``：全量。

    判定顺序（**故意**保守，宁可多导也别把主数据切没了）：
    1. 维度表（``DIM_`` 前缀）⇒ 全量；
    2. 真业务日期列 ⇒ 用它（按列的**实际类型**决定 date / text-date）；
    3. 其它**非技术性** DATE/TIMESTAMP 列 ⇒ 用它；
    4. ``YMONTH`` ⇒ 用它；
    5. 都没有 ⇒ 全量。
    """
    byName = {c["name"].upper(): c for c in columns}
    if tableName.upper().startswith(_DIMENSION_TABLE_PREFIXES):
        return None, "none"
    for candidate in _BUSINESS_DATE_COLUMNS:
        column = byName.get(candidate)
        if column is None:
            continue
        dataType = str(column["type"]).upper()
        if dataType.startswith(("DATE", "TIMESTAMP")):
            return column["name"], "date"
        # ⚠️ 同名列**可能是 VARCHAR**（ODS 层的 SRM 表就是这样：
        # ODS_SRM_RECEIPT_FINANCE_DETAIL.RECEIPT_DATE 是 VARCHAR）——
        # 直接拿它和 DATE '2025-01-01' 比较 ⇒ Oracle 隐式 TO_DATE ⇒ **ORA-01861** 整轮挂。
        # ⇒ VARCHAR 一律走 text-date（字符串比较），格式在主循环里实测确认。
        return column["name"], "text-date"
    technical = set(_TECHNICAL_DATE_COLUMNS)
    for column in columns:
        name = str(column["name"]).upper()
        dataType = str(column["type"]).upper()
        if name in technical:
            continue
        if dataType.startswith(("DATE", "TIMESTAMP")):
            return column["name"], "date"
    ymonth = byName.get("YMONTH")
    if ymonth is not None:
        return ymonth["name"], "ym"
    return None, "none"


def _filterClause(columnName: str, kind: str, fromYear: int) -> str:
    """时间过滤子句（≥ fromYear-01-01）。

    ⚠️ Oracle 用 **1600-01-03** 表示「空日期」⇒ `>= 2025-01-01` 天然排除这些哨兵行
    （实测 DWD_PURCHASE_ORDER_DTL.ORDER_DATE 最小值就是 1600-01-03）。
    ⚠️ `YMONTH` 格式**两张表不一样**（'2025-01' vs '202501'）⇒ 按 kind 分别拼。
    ⚠️ ``text-date``（VARCHAR 存的 ISO 日期）**只能字符串比较** ——
    拿它和 DATE 字面量比 ⇒ Oracle 隐式 TO_DATE ⇒ ORA-01861。
    """
    quoted = _quote(columnName)
    if kind == "date":
        return f"{quoted} >= DATE '{fromYear}-01-01'"
    if kind == "ym-dash":
        return f"{quoted} >= '{fromYear}-01'"
    if kind == "ym-plain":
        return f"{quoted} >= '{fromYear}01'"
    if kind == "text-date":
        return f"{quoted} >= '{fromYear}-01-01'"
    raise ValueError(f"未知的过滤类型：{kind}")


async def _detectYmKind(cursor, owner: str, table: str, column: str) -> str:
    """探测 YMONTH 列的**实际字符串格式** ⇒ ``ym-dash``（'2025-01'）/ ``ym-plain``（'202501'）。

    ⚠️ **两种格式在 THBI 里同时存在**（实测 19 张含 YMONTH 的表里，
    **只有 ``DWS_PRICE_MONTHLY`` 是 ``ym-plain``**，其余 18 张都是 ``ym-dash``）——
    字符串比较下 ``'2016-07' >= '202501'`` 为 **False** ⇒ 用错格式会把**整表滤成 0 行**。
    ⇒ 每次都按表实测，不写死。
    """
    await cursor.execute(
        f'SELECT MAX({_quote(column)}) FROM {_quote(owner)}.{_quote(table)} '
        f'WHERE {_quote(column)} IS NOT NULL'
    )
    row = await cursor.fetchone()
    sample = row[0] if row else None
    text = str(sample) if sample is not None else ""
    if text.isdigit() and len(text) == 6:
        return "ym-plain"
    if len(text) == 7 and text[4] == "-":
        return "ym-dash"
    return "ym-unknown"


async def _detectTextDateKind(cursor, owner: str, table: str, column: str) -> str:
    """探测 VARCHAR 日期列的实际格式 ⇒ ``text-date``（ISO '2025-01-01'）/ ``text-unknown``。

    ⚠️ 只有 **ISO 格式**才能安全地做字符串比较（字典序 == 时间序）；
    其它格式（EBS 的 '01-JAN-25'、'2025/01/01'…）一律 ``text-unknown``
    ⇒ 该表**不做过滤**（导全量），宁可多导也别错滤。
    """
    await cursor.execute(
        f'SELECT MAX({_quote(column)}) FROM {_quote(owner)}.{_quote(table)} '
        f'WHERE {_quote(column)} IS NOT NULL'
    )
    row = await cursor.fetchone()
    sample = row[0] if row else None
    text = str(sample) if sample is not None else ""
    if len(text) >= 10 and text[4] == "-" and text[7] == "-" and text[:4].isdigit():
        return "text-date"
    return "text-unknown"


def _hashExpr(column: dict[str, Any]) -> str:
    """列 → 可哈希的字符串表达式（NULL → '~'）。

    ⚠️ **RAW/BINARY 列不能用 `TO_CHAR`**（Oracle: ORA-00932「应为 NUMBER, 却获得
    BINARY」）⇒ RAW 族走 `RAWTOHEX`（十六进制字符串）。
    """
    name = _quote(column["name"])
    dataType = str(column["type"]).upper()
    if dataType.startswith("RAW") or dataType == "ROWID":
        return f"NVL(RAWTOHEX({name}),'~')"
    return f"NVL(TO_CHAR({name}),'~')"


async def _columnsOf(cursor, owner: str) -> list[dict[str, Any]]:
    """读某 schema 的全部表 + 列（含 LOB 标记）。"""
    await cursor.execute(
        """
SELECT t.table_name, c.column_name, c.data_type, c.nullable, c.data_length,
       c.data_precision, c.data_scale, c.column_id
FROM all_tables t
JOIN all_tab_columns c ON c.owner = t.owner AND c.table_name = t.table_name
WHERE t.owner = :owner
ORDER BY t.table_name, c.column_id
""",
        owner=owner,
    )
    tables: dict[str, list[dict[str, Any]]] = {}
    for row in await cursor.fetchall():
        name, column, dataType, nullable, length, precision, scale, _ = row
        tables.setdefault(name, []).append(
            {
                "name": column,
                "type": dataType,
                "nullable": nullable == "Y",
                "length": length,
                "precision": precision,
                "scale": scale,
            }
        )
    return [{"name": k, "columns": v} for k, v in sorted(tables.items())]


def _typeSpec(column: dict[str, Any]) -> str:
    """列类型 → DDL 片段（Oracle 原生类型，不追求 1:1 还原约束，只保结构可读）。"""
    dataType = str(column["type"]).upper()
    if dataType in ("NUMBER", "NUMERIC", "FLOAT"):
        if column.get("precision") is not None:
            if column.get("scale") is not None:
                return f"NUMBER({column['precision']},{column['scale']})"
            return f"NUMBER({column['precision']})"
        return "NUMBER"
    if dataType in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"):
        return f"{dataType}({column.get('length') or 1})"
    if dataType == "TIMESTAMP":
        return "TIMESTAMP"
    return dataType


async def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle schema 级备份（只读）")
    parser.add_argument("--datasource-id", type=int, default=1, help="元数据库里的数据源 id")
    parser.add_argument("--schema", default="THBI", help="要备份的 owner（schema）")
    parser.add_argument("--out", default="../backups/oracle", help="输出目录")
    parser.add_argument("--only-tables", default="", help="逗号分隔的表名（默认全部）")
    parser.add_argument(
        "--skip-tables",
        type=int,
        default=0,
        help="跳过按字母序的前 N 张表（**断点续传**：上轮超时被打断时，接着跑剩下的）",
    )
    parser.add_argument(
        "--from-year",
        type=int,
        default=0,
        help="只导该年（含）之后的数据；0=不限。过滤列按业务日期优先级自动挑",
    )
    parser.add_argument(
        "--structure-only",
        action="store_true",
        help="只导结构（DDL）不导数据 —— 全 schema 4600 万行时用这个",
    )
    parser.add_argument(
        "--fingerprint",
        action="store_true",
        help="只出**指纹**（行数 + 非 LOB 列内容哈希和）——「怕源表被误改」的留底形态："
        "秒级可复算比对，不需要几十 GB 的全量 dump",
    )
    parser.add_argument(
        "--fingerprint-tables",
        default="",
        help="指纹范围：逗号分隔的表名（默认 = 脚本读到的全部表）",
    )
    parser.add_argument(
        "--max-rows-per-table",
        type=int,
        default=0,
        help="每表最多导多少行（0=不限）—— 小样本抽样用",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只决定「每张表用哪一列做时间过滤 / 是否全量」，**不导数据** —— 改过滤规则后先跑它",
    )
    args = parser.parse_args()

    outDir = Path(args.out).resolve()
    outDir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    sqlPath = outDir / f"{args.schema}_schema_{stamp}.sql"
    manifestPath = outDir / f"{args.schema}_schema_{stamp}.manifest.json"

    # 凭据从元数据库里的数据源行取（口令用仓内解密）
    from sqlalchemy import select

    from app.domain.models import DataSource
    from app.infrastructure.database import getSessionFactory
    from app.infrastructure.security.crypto import decryptApiKey

    async def loadDatasource() -> Any:
        factory = getSessionFactory()
        async with factory() as session:
            row = (
                await session.execute(
                    select(DataSource).where(DataSource.id == args.datasource_id)
                )
            ).scalars().first()
            if row is None:
                raise SystemExit(f"数据源 {args.datasource_id} 不存在")
            return row

    ds = await loadDatasource()
    password = decryptApiKey(ds.password_encrypted)
    dsn = f"{ds.host}:{ds.port}/{ds.database_name}"

    owner = args.schema.upper()

    connection = await oracledb.connect_async(
        user=ds.username, password=password, dsn=dsn
    )
    manifest: list[dict[str, Any]] = []
    try:
        cursor = connection.cursor()
        tables = await _columnsOf(cursor, owner)
        wanted = {t.strip().upper() for t in args.only_tables.split(",") if t.strip()}
        if wanted:
            tables = [t for t in tables if t["name"].upper() in wanted]
        if args.skip_tables > 0:
            skipped = args.skip_tables
            tables = tables[skipped:]
            print(
                f"[backup_oracle_schema] 跳过前 {skipped} 张表（续传），本轮 {len(tables)} 张"
            )

        if args.fingerprint:
            wantedTables = {
                t.strip().upper()
                for t in args.fingerprint_tables.split(",")
                if t.strip()
            }
            fingerprints = []
            for table in tables:
                name = table["name"]
                if wantedTables and name.upper() not in wantedTables:
                    continue
                columns = table["columns"]
                hashable = [
                    c for c in columns if c["type"].upper() not in _SKIP_COLUMN_TYPES
                ]
                countSql = f"SELECT COUNT(*) FROM {_quote(owner)}.{_quote(name)}"
                await cursor.execute(countSql)
                rowCount = int((await cursor.fetchone())[0])
                # ⚠️ `STANDARD_HASH` 返回 RAW，`SUM()` 要 NUMBER ⇒ ORA-00932。
                # 改用 `ORA_HASH`（返回 NUMBER）：**按列**各算一个聚合，一次扫描覆盖全部列
                # （比「把整行拼起来再哈希」更敏感 —— 拼接超 4000 字节会截断）。
                columnSums: dict[str, str] = {}
                if hashable:
                    selectParts = ["COUNT(*)"] + [
                        f"SUM(NVL(ORA_HASH({_hashExpr(c)}),0))" for c in hashable
                    ]
                    hashSql = (
                        f"SELECT {', '.join(selectParts)} FROM {_quote(owner)}.{_quote(name)}"
                    )
                    await cursor.execute(hashSql)
                    values = await cursor.fetchone()
                    rowCount = int(values[0] or 0)
                    columnSums = {
                        c["name"]: str(values[pos + 1] or 0)
                        for pos, c in enumerate(hashable)
                    }
                fingerprints.append(
                    {
                        "table": name,
                        "rows": rowCount,
                        "columnHashSums": columnSums,
                        "hashedColumns": len(hashable),
                        "skippedColumns": [
                            c["name"] for c in columns if c not in hashable
                        ],
                    }
                )
                print(
                    f"  {name:42s} rows={rowCount:>10,} 列哈希={len(columnSums)}",
                    flush=True,
                )
            fingerprintPath = outDir / f"{owner}_fingerprint_{stamp}.json"
            fingerprintPath.write_text(
                json.dumps(
                    {
                        "schema": owner,
                        "datasource": {
                            "id": args.datasource_id,
                            "host": ds.host,
                            "port": ds.port,
                        },
                        "generatedAt": datetime.now(UTC).isoformat(),
                        "note": (
                            "行数 + 非 LOB/日期列 STANDARD_HASH 求和；"
                            "复算同口径即可判定源表是否被改动"
                        ),
                        "tables": fingerprints,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"[backup_oracle_schema] 指纹: {fingerprintPath}")
            print(f"[backup_oracle_schema] 表={len(fingerprints)}")
            cursor.close()
            return 0

        with sqlPath.open("w", encoding="utf-8") as out:
            out.write(f"-- Oracle schema 备份：{owner} @ {ds.host}:{ds.port}/{ds.database_name}\n")
            out.write(f"-- 生成时间：{datetime.now(UTC).isoformat()}\n")
            out.write("-- 由 scripts/backup_oracle_schema.py 生成（只读导出版本）\n")
            out.write("SET DEFINE OFF;\nWHENEVER SQLERROR EXIT SQL.SQLCODE;\n\n")

            for table in tables:
                name = table["name"]
                columns = table["columns"]
                dataColumns = [c for c in columns if c["type"].upper() not in _SKIP_COLUMN_TYPES]
                skipped = [c["name"] for c in columns if c["type"].upper() in _SKIP_COLUMN_TYPES]

                columnSql = ",\n  ".join(
                    f"{_quote(c['name'])} {_typeSpec(c)}"
                    + ("" if c["nullable"] else " NOT NULL")
                    for c in columns
                )
                out.write(f"-- ===== {owner}.{name} =====\n")
                out.write(f"CREATE TABLE {_quote(name)} (\n  {columnSql}\n);\n")

                if args.structure_only or not dataColumns:
                    manifest.append(
                        {
                            "table": name,
                            "rows": 0,
                            "columns": len(columns),
                            "skippedLobColumns": skipped,
                            "note": "整表只有 LOB/ROWID 列，未导数据",
                        }
                    )
                    continue

                columnList = ", ".join(_quote(c["name"]) for c in dataColumns)
                selectSql = (
                    f"SELECT {columnList} FROM {_quote(owner)}.{_quote(name)}"
                )
                filterColumn = None
                filterKind = "none"
                if args.from_year > 0:
                    filterColumn, filterKind = _pickFilterColumn(name, columns)
                    if filterColumn is not None:
                        if filterKind == "ym":
                            filterKind = await _detectYmKind(
                                cursor, owner, name, filterColumn
                            )
                        elif filterKind == "text-date":
                            filterKind = await _detectTextDateKind(
                                cursor, owner, name, filterColumn
                            )
                        if filterKind in ("ym-unknown", "text-unknown"):
                            # 格式认不出来 ⇒ 不冒险过滤，直接导全量
                            filterColumn = None
                            filterKind = "none"
                        else:
                            selectSql += (
                                " WHERE "
                                + _filterClause(
                                    filterColumn, filterKind, args.from_year
                                )
                            )
                if args.dry_run:
                    manifest.append(
                        {
                            "table": name,
                            "rows": 0,
                            "columns": len(columns),
                            "skippedLobColumns": skipped,
                            "filterColumn": filterColumn,
                            "filterKind": filterKind,
                            "dryRun": True,
                        }
                    )
                    continue
                await cursor.execute(selectSql)
                rows = await cursor.fetchall()
                fallbackFullExport = False
                # ⚠️ 兜底：按时间过滤后 **0 行** ⇒ 该表大概率是**存量/主数据表**
                # （业务日期列选错或数据本来就老）⇒ 回退成**全量导出**，
                # 否则备份里会留下一张空表，恢复后关联不上。
                if not rows and filterColumn is not None:
                    await cursor.execute(
                        f"SELECT {columnList} FROM {_quote(owner)}.{_quote(name)}"
                    )
                    rows = await cursor.fetchall()
                    fallbackFullExport = True
                    filterColumn = None
                    filterKind = "none"
                index = [c["name"] for c in dataColumns]
                for row in rows:
                    values = ", ".join(_literal(row[pos]) for pos in range(len(index)))
                    out.write(f"INSERT INTO {_quote(name)} ({columnList}) VALUES ({values});\n")
                manifest.append(
                    {
                        "table": name,
                        "rows": len(rows),
                        "columns": len(columns),
                        "skippedLobColumns": skipped,
                        "filterColumn": filterColumn,
                        "filterKind": filterKind,
                        "fallbackFullExport": fallbackFullExport,
                    }
                )
                if skipped:
                    out.write(f"-- ⚠️ LOB 列未导数据（需 expdp 才有）：{', '.join(skipped)}\n")
                if filterColumn:
                    out.write(
                        f"-- 时间过滤：{filterColumn} >= {args.from_year}-01-01\n"
                    )
                elif fallbackFullExport:
                    out.write(
                        "-- ⚠️ 时间过滤后 0 行 ⇒ 已**回退为全量导出**（该表疑似存量/主数据表）\n"
                    )
                else:
                    out.write("-- ⚠️ 无可用日期列 ⇒ 本表**未做时间过滤**（导出全量）\n")
            out.write("\nCOMMIT;\nEXIT;\n")
        cursor.close()
    finally:
        await connection.close()

    totalRows = sum(int(item["rows"]) for item in manifest)
    manifestPath.write_text(
        json.dumps(
            {
                "schema": owner,
                "datasource": {"id": args.datasource_id, "host": ds.host, "port": ds.port},
                "generatedAt": datetime.now(UTC).isoformat(),
                "tableCount": len(manifest),
                "rowCount": totalRows,
                "tables": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[backup_oracle_schema] schema={owner} 表={len(manifest)} 行={totalRows}")
    print(f"[backup_oracle_schema] SQL    : {sqlPath}")
    print(f"[backup_oracle_schema] manifest: {manifestPath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
