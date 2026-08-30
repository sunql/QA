#!/usr/bin/env python3
"""THBI 数仓建仓执行器。

用法（backend venv，密码走环境变量，绝不写死）：
    cd backend && THBI_PASSWORD=*** ./.venv/bin/python ../dw/run_build.py --phase all
    THBI_PASSWORD=*** ./.venv/bin/python ../dw/run_build.py --phase dwd,dws
    THBI_PASSWORD=*** ./.venv/bin/python ../dw/run_build.py --phase all --verify
    THBI_PASSWORD=*** ./.venv/bin/python ../dw/run_build.py --incremental          # ODS/DWD 增量 MERGE
    THBI_PASSWORD=*** ./.venv/bin/python ../dw/run_build.py --incremental --verify

行为：
- 默认全量快照：按 phase 顺序执行 dw/*.sql，逐语句提交并打印耗时/影响行数
- CREATE 前自动 drop-if-exists（幂等可重跑）
- --verify 时对 ODS 层做与 ZJTH 源表的行数对账
- --incremental：ODS/DWD 按 UPDDATTIM_0 水位 MERGE 增量（DWD 表无 PK 时全量 reload；
  DIM/DWS/ADS 保持全量重建）。水位存 THBI.ETL_WATERMARK，首次增量从 1900-01-01 起
  全量 MERGE 后推进到源 MAX(UPDDATTIM_0)。要求 ODS/DWD 表已存在（先跑一次全量）。
  注意：MERGE 只处理插入/更新，不处理源表物理删除的行（X3 多以状态字段软删）。
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time
from pathlib import Path

import oracledb

DW_DIR = Path(__file__).resolve().parent
ORACLE_DSN = {"host": "192.168.205.70", "port": 1521, "service_name": "X3V71ORA"}

# dwd_merge 在 backend/scripts（dwd_spec 同目录）；保证 scripts 包可导入
sys.path.insert(0, str(DW_DIR.parent / "backend" / "scripts"))
from dwd_merge import (  # noqa: E402
    buildDwdMerge,
    buildOdsMerge,
    buildOdsWatermarkQuery,
    buildReload,
    buildWatermarkQuery,
    parseCreatePks,
    parseDwdInserts,
)

WATERMARK_DDL = """CREATE TABLE THBI.ETL_WATERMARK (
  target_table   VARCHAR2(60) PRIMARY KEY,
  last_watermark TIMESTAMP(3),
  last_sync_ts   TIMESTAMP(3) DEFAULT SYSTIMESTAMP
)"""

# ZJTH 源表无唯一索引的表 -> 手动 MERGE 键（大写 X3 列名）
MANUAL_ODS_KEYS = {"ITMMASTER": ["ITMREF_0"]}

# 首次增量（无水位）时的低水位：全量 MERGE
EPOCH_WM = datetime.datetime(1900, 1, 1)

PHASE_FILES = {
    "ods": DW_DIR / "01_ods.sql",
    "dwd": [DW_DIR / "02_dwd_master.sql", DW_DIR / "03_dwd_facts.sql"],
    "dim": DW_DIR / "04_dim.sql",
    "dws": DW_DIR / "05_dws.sql",
    "ads": DW_DIR / "06_ads.sql",
}

ODS_TABLES = [
    "BPSUPPLIER", "BPARTNER", "BPCUSTOMER", "BPCARRIER", "ITMMASTER",
    "FACILITY", "ITMFACILIT", "PORDER", "PORDERQ", "PREQUISD", "PREQUISO",
    "PQUOTAT", "PQUOTATD", "YPRECEIPT", "YPRECEIPTD", "PRECEIPT",
    "PRECEIPTD", "PINVOICE", "PINVOICED", "PAYMENTH", "PAYMENTD",
    "PPRICCONF", "PPRICFICH", "PPRICLIST", "BOM", "BOMD", "ROUOPE",
]

CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+VIEW|TABLE|VIEW|SEQUENCE|INDEX)\s+THBI\.(\w+)",
    re.IGNORECASE,
)


def splitStatements(sqlText: str) -> list[str]:
    """按 ';' 切分语句；先剔除整行注释与空行，避免注释中的分号干扰。"""
    lines = [ln for ln in sqlText.splitlines()
             if ln.strip() and not ln.strip().startswith("--")]
    cleaned = "\n".join(lines)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


def targetObjectType(stmt: str) -> tuple[str, str] | None:
    """从 CREATE 语句提取 (对象类型, 对象名)，用于 drop-if-exists。"""
    m = CREATE_RE.search(stmt)
    if not m:
        return None
    name = m.group(1)
    kind = m.group(0).upper()
    if "VIEW" in kind:
        return ("VIEW", name)
    if "SEQUENCE" in kind:
        return ("SEQUENCE", name)
    if "INDEX" in kind:
        return ("INDEX", name)
    return ("TABLE", name)


def dropIfExists(conn: oracledb.Connection, objType: str, name: str) -> None:
    """对象存在则 DROP（表随删；索引随表自动删除，无需单独处理）。"""
    if objType == "INDEX":
        return
    if objType == "TABLE":
        catalog, col = "USER_TABLES", "TABLE_NAME"
    elif objType == "VIEW":
        catalog, col = "USER_VIEWS", "VIEW_NAME"
    else:
        catalog, col = "USER_SEQUENCES", "SEQUENCE_NAME"
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {catalog} WHERE {col} = :n",
                    n=name.upper())
        if cur.fetchone()[0] == 0:
            return
        cur.execute(f"DROP {objType} THBI.{name.upper()}")
        conn.commit()
        print(f"  [drop] {objType} {name}")


def executePhase(conn: oracledb.Connection, phase: str, files: list[Path]) -> None:
    print(f"\n=== phase {phase} ===")
    for f in files:
        print(f"-- {f.name}")
        for stmt in splitStatements(f.read_text(encoding="utf-8")):
            target = targetObjectType(stmt)
            if target:
                dropIfExists(conn, *target)
            started = time.monotonic()
            try:
                with conn.cursor() as cur:
                    cur.execute(stmt)
                    rowcount = cur.rowcount
                conn.commit()
            except oracledb.DatabaseError as exc:
                conn.rollback()
                print(f"  [FAIL] {stmt[:120]}...")
                raise SystemExit(f"SQL 失败（phase {phase}）: {exc}") from exc
            label = target[1] if target else stmt.split("\n")[0][:60]
            elapsed = time.monotonic() - started
            print(f"  [ok] {label:<40} rows={rowcount:<10} {elapsed:.1f}s")


def verifyOds(conn: oracledb.Connection) -> None:
    print("\n=== verify: ODS 行数对账（THBI vs ZJTH） ===")
    failed = 0
    with conn.cursor() as cur:
        for t in ODS_TABLES:
            cur.execute(f"SELECT COUNT(*) FROM THBI.ODS_{t}")
            ods = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM ZJTH.{t}")
            src = cur.fetchone()[0]
            status = "OK" if ods == src else "MISMATCH"
            if ods != src:
                failed += 1
            print(f"  {t:<12} ods={ods:>10,}  src={src:>10,}  {status}")
    if failed:
        raise SystemExit(f"对账失败：{failed} 张表行数不一致")
    print("  全部一致")


def verifyCleaning(conn: oracledb.Connection) -> None:
    print("\n=== verify: DWD 清洗校验 ===")
    checks = [
        ("DWD 无 1599 日期残留",
         "SELECT COUNT(*) FROM THBI.DWD_PURCHASE_ORDER_LINE "
         "WHERE promised_receipt_date = DATE '1599-12-31' "
         "OR requested_receipt_date = DATE '1599-12-31'"),
        ("DWD 收货行无空串 PO 引用",
         "SELECT COUNT(*) FROM THBI.DWD_GOODS_RECEIPT_LINE "
         "WHERE po_no = ' '"),
        ("DWD 发票行无空串 PO 引用",
         "SELECT COUNT(*) FROM THBI.DWD_PURCHASE_INVOICE_LINE "
         "WHERE po_no = ' '"),
        ("DIM_DATE 覆盖 2012-2027",
         "SELECT COUNT(*) FROM THBI.DIM_DATE "
         "WHERE calendar_date BETWEEN DATE '2012-01-01' AND DATE '2027-12-31'"),
    ]
    with conn.cursor() as cur:
        for label, sql in checks:
            cur.execute(sql)
            print(f"  {label}: {cur.fetchone()[0]}")
    # 抽样 OTD 链路
    print("  OTD 抽样（收货行 vs PO 承诺日期）:")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT g.receipt_no, g.po_no, g.receipt_date, p.promised_receipt_date, "
            "CASE WHEN g.receipt_date <= p.promised_receipt_date THEN 'ON_TIME' "
            "ELSE 'LATE' END FROM THBI.DWD_GOODS_RECEIPT_LINE g "
            "JOIN THBI.DWD_PURCHASE_ORDER_LINE p "
            "ON p.po_no = g.po_no AND p.po_line_no = g.po_line_no "
            "WHERE g.receipt_date IS NOT NULL AND ROWNUM <= 5")
        for row in cur.fetchall():
            print(f"    {row}")


# ---------------------------------------------------------------- 增量同步 --

def ensureWatermarkTable(conn: oracledb.Connection) -> None:
    """水位控制表不存在则创建（幂等）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME = 'ETL_WATERMARK'")
        if cur.fetchone()[0] == 0:
            cur.execute(WATERMARK_DDL)
            conn.commit()
            print("  [create] THBI.ETL_WATERMARK")


def getWatermark(conn: oracledb.Connection, target: str) -> datetime.datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_watermark FROM THBI.ETL_WATERMARK WHERE target_table = :t",
            t=target,
        )
        row = cur.fetchone()
        return row[0] if row else None


def setWatermark(conn: oracledb.Connection, target: str, ts: datetime.datetime) -> None:
    """写入/更新某目标表的同步水位（源 MAX(UPDDATTIM_0)）。"""
    with conn.cursor() as cur:
        cur.execute(
            "MERGE INTO THBI.ETL_WATERMARK t "
            "USING (SELECT :t AS tt, :w AS wm FROM DUAL) s "
            "ON (t.target_table = s.tt) "
            "WHEN MATCHED THEN UPDATE SET t.last_watermark = s.wm "
            "WHEN NOT MATCHED THEN INSERT (target_table, last_watermark) "
            "VALUES (s.tt, s.wm)",
            t=target, w=ts,
        )
    conn.commit()


def uniqueIndexCols(conn: oracledb.Connection, owner: str, table: str) -> list[str]:
    """源表唯一索引列清单（列数最少者优先）；无唯一索引返回 []。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.INDEX_NAME, COUNT(ic.COLUMN_NAME) AS ncols "
            "FROM ALL_INDEXES i "
            "JOIN ALL_IND_COLUMNS ic "
            "  ON ic.INDEX_NAME = i.INDEX_NAME AND ic.TABLE_OWNER = i.TABLE_OWNER "
            "WHERE i.TABLE_OWNER = :o AND i.TABLE_NAME = :t AND i.UNIQUENESS = 'UNIQUE' "
            "GROUP BY i.INDEX_NAME ORDER BY ncols, i.INDEX_NAME",
            o=owner, t=table,
        )
        row = cur.fetchone()
    if not row:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM ALL_IND_COLUMNS "
            "WHERE TABLE_OWNER = :o AND INDEX_NAME = :i ORDER BY COLUMN_POSITION",
            o=owner, i=row[0],
        )
        return [r[0] for r in cur.fetchall()]


def syncOds(conn: oracledb.Connection, source: str) -> None:
    """单张 ODS 表增量：从 ZJTH 源表按 UPDDATTIM_0 水位 MERGE。"""
    target = f"ODS_{source}"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM USER_TAB_COLUMNS WHERE TABLE_NAME = :t "
            "ORDER BY COLUMN_ID",
            t=target,
        )
        cols = [r[0] for r in cur.fetchall()]
    if not cols:
        raise SystemExit(f"{target} 不存在，请先执行全量建仓（--phase all）")
    pk = MANUAL_ODS_KEYS.get(source) or uniqueIndexCols(conn, "ZJTH", source)
    if not pk:
        raise SystemExit(f"{source}: ZJTH 无唯一索引且未配置手动 MERGE 键，无法增量")
    wm = getWatermark(conn, target) or EPOCH_WM
    sql = buildOdsMerge(source, target, cols, pk)
    started = time.monotonic()
    with conn.cursor() as cur:
        cur.execute(sql, wm=wm)
        n = cur.rowcount
        cur.execute(buildOdsWatermarkQuery(source))
        new_wm = cur.fetchone()[0]
    setWatermark(conn, target, new_wm)
    print(f"  [ok] {target:<24} merged={n:<10} wm={wm} -> {new_wm} "
          f"{time.monotonic() - started:.1f}s")


def syncDwd(conn: oracledb.Connection, info, pk_cols: list[str]) -> None:
    """单张 DWD 表增量：有 PK 走 MERGE，无 PK（表小）走全量 reload。"""
    target = info.target
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME = :t", t=target)
        if cur.fetchone()[0] == 0:
            raise SystemExit(f"{target} 不存在，请先执行全量建仓（--phase all）")
    wm = getWatermark(conn, target) or EPOCH_WM
    started = time.monotonic()
    if not pk_cols:
        # 无 PK（BOM/BOM_DETAIL/ROUTING_OPERATION，小表）：全量重灌，语义与全量一致
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM THBI.{target}")
            cur.execute(buildReload(info))
            n = cur.rowcount
            cur.execute(buildWatermarkQuery(info))
            new_wm = cur.fetchone()[0]
        setWatermark(conn, target, new_wm)
        kind = f"reload={n}"
    else:
        with conn.cursor() as cur:
            cur.execute(buildDwdMerge(info, pk_cols), wm=wm)
            n = cur.rowcount
            cur.execute(buildWatermarkQuery(info))
            new_wm = cur.fetchone()[0]
        setWatermark(conn, target, new_wm)
        kind = f"merged={n} wm={wm} -> {new_wm}"
    print(f"  [ok] {target:<38} {kind:<30} {time.monotonic() - started:.1f}s")


def runIncremental(conn: oracledb.Connection, phases: list[str]) -> None:
    """增量同步：ODS/DWD 按 UPDDATTIM_0 水位 MERGE，DIM/DWS/ADS 全量重建。"""
    ensureWatermarkTable(conn)
    if "ods" in phases:
        print("\n=== incremental: ODS MERGE ===")
        for src in ODS_TABLES:
            syncOds(conn, src)
    if "dwd" in phases:
        print("\n=== incremental: DWD MERGE ===")
        sql_text = "\n".join(
            f.read_text(encoding="utf-8") for f in PHASE_FILES["dwd"]
        )
        inserts = parseDwdInserts(sql_text)
        pks = parseCreatePks(sql_text)
        for table, info in inserts.items():
            syncDwd(conn, info, pks.get(table, []))
    for phase in ("dim", "dws", "ads"):
        if phase in phases:
            spec = PHASE_FILES[phase]
            files = spec if isinstance(spec, list) else [spec]
            executePhase(conn, phase, files)


def main() -> None:
    parser = argparse.ArgumentParser(description="THBI 数仓建仓")
    parser.add_argument("--phase", default="all",
                        help="逗号分隔：ods,dwd,dim,dws,ads 或 all")
    parser.add_argument("--incremental", action="store_true",
                        help="增量同步：ODS/DWD 按 UPDDATTIM_0 水位 MERGE，"
                             "DIM/DWS/ADS 全量重建")
    parser.add_argument("--verify", action="store_true",
                        help="只跑校验（ODS 对账 + 清洗校验 + OTD 抽样）")
    args = parser.parse_args()

    password = os.environ.get("THBI_PASSWORD")
    if not password:
        raise SystemExit("环境变量 THBI_PASSWORD 未设置（THBI 用户密码）")

    conn = oracledb.connect(user="THBI", password=password, **ORACLE_DSN)
    try:
        if args.verify and not args.incremental:
            verifyOds(conn)
            verifyCleaning(conn)
            return

        phases = (["ods", "dwd", "dim", "dws", "ads"]
                  if args.phase == "all" else args.phase.split(","))
        unknown = [p for p in phases if p not in PHASE_FILES]
        if unknown:
            raise SystemExit(f"未知 phase: {unknown}（可选 ods,dwd,dim,dws,ads）")

        if args.incremental:
            runIncremental(conn, phases)
            if args.verify:
                verifyOds(conn)
                verifyCleaning(conn)
            print("\n=== 增量同步完成 ===")
            return

        for phase in phases:
            spec = PHASE_FILES[phase]
            files = spec if isinstance(spec, list) else [spec]
            executePhase(conn, phase, files)

        print("\n=== 建仓完成 ===")
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME LIKE 'ODS_%'")
            print(f"  ODS 表: {cur.fetchone()[0]}")
            cur.execute("SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME LIKE 'DWD_%'")
            print(f"  DWD 表: {cur.fetchone()[0]}")
            cur.execute("SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME LIKE 'DIM_%'")
            print(f"  DIM 表: {cur.fetchone()[0]}")
            cur.execute("SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME LIKE 'DWS_%'")
            print(f"  DWS 表: {cur.fetchone()[0]}")
            cur.execute("SELECT COUNT(*) FROM USER_VIEWS WHERE VIEW_NAME LIKE 'ADS_%'")
            print(f"  ADS 视图: {cur.fetchone()[0]}")
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
