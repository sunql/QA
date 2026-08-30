#!/usr/bin/env python3
"""THBI 数仓建仓执行器。

用法（backend venv，密码走环境变量，绝不写死）：
    cd backend && THBI_PASSWORD=*** ./.venv/bin/python ../dw/run_build.py --phase all
    THBI_PASSWORD=*** ./.venv/bin/python ../dw/run_build.py --phase dwd,dws
    THBI_PASSWORD=*** ./.venv/bin/python ../dw/run_build.py --phase all --verify

行为：
- 按 phase 顺序执行 dw/*.sql，逐语句提交并打印耗时/影响行数
- CREATE 前自动 drop-if-exists（幂等可重跑）
- --verify 时对 ODS 层做与 ZJTH 源表的行数对账
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import oracledb

DW_DIR = Path(__file__).resolve().parent
ORACLE_DSN = {"host": "192.168.205.70", "port": 1521, "service_name": "X3V71ORA"}

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


def main() -> None:
    parser = argparse.ArgumentParser(description="THBI 数仓建仓")
    parser.add_argument("--phase", default="all",
                        help="逗号分隔：ods,dwd,dim,dws,ads 或 all")
    parser.add_argument("--verify", action="store_true",
                        help="只跑校验（ODS 对账 + 清洗校验 + OTD 抽样）")
    args = parser.parse_args()

    password = os.environ.get("THBI_PASSWORD")
    if not password:
        raise SystemExit("环境变量 THBI_PASSWORD 未设置（THBI 用户密码）")

    conn = oracledb.connect(user="THBI", password=password, **ORACLE_DSN)
    try:
        if args.verify:
            verifyOds(conn)
            verifyCleaning(conn)
            return

        phases = (["ods", "dwd", "dim", "dws", "ads"]
                  if args.phase == "all" else args.phase.split(","))
        unknown = [p for p in phases if p not in PHASE_FILES]
        if unknown:
            raise SystemExit(f"未知 phase: {unknown}（可选 ods,dwd,dim,dws,ads）")

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
