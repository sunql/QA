"""Dump Oracle business DB schema for the 8 core tables (+ ROUOPE).

Reads table columns and foreign-key constraints from ZJTH schema,
writes a structured report to /tmp/oracle_schema_output.txt.
"""
import os
import sys

import oracledb

DSN = "192.168.205.70:1521/X3V71ORA"
USER = "ZJTH"
# 口令不硬编码：经环境变量注入（可由应用 SECRET_KEY 解密 data_source.password_encrypted 取得）
PWD = os.environ.get("ORACLE_PASSWORD")
if not PWD:
    print("缺少 ORACLE_PASSWORD 环境变量（ZJTH 业务库口令，勿硬编码于脚本）", file=sys.stderr)
    sys.exit(1)

TABLES = [
    "ITMMASTER",   # 物料/产品表
    "BPCUSTOMER",  # 客户表
    "BPARTNER",    # 合作伙伴表
    "BPSUPPLIER",  # 供应商表
    "BOM",         # BOM表
    "BOMD",        # BOM明细表
    "ITMFACILIT",  # 物料地点表
    "FACILITY",    # 地点信息表
    "ROUOPE",      # 工艺路线
    "ATEXTRA",     # 文本/字典
]

out = open("/tmp/oracle_schema_output.txt", "w", encoding="utf-8")

def p(*a):
    print(*a, file=out)

try:
    conn = oracledb.connect(user=USER, password=PWD, dsn=DSN)
except Exception as e:
    print(f"CONNECT FAILED: {e}", file=sys.stderr)
    sys.exit(1)

cur = conn.cursor()

p("=" * 70)
p("ORACLE SCHEMA DUMP — ZJTH")
p("=" * 70)

# 1. Columns per table
for t in TABLES:
    try:
        cur.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = 'ZJTH' AND TABLE_NAME = :t
            ORDER BY COLUMN_ID
            """,
            t=t,
        )
        rows = cur.fetchall()
    except Exception as e:
        p(f"\n### {t}: QUERY ERROR {e}")
        continue
    p(f"\n### {t}  ({len(rows)} columns)")
    p("-" * 60)
    for col_name, dtype, dlen, nullable in rows:
        p(f"  {col_name:28s} {dtype:18s} ({dlen:>5}) {'NULL' if nullable == 'Y' else 'NOT NULL'}")

# 2. Row counts (skip ATEXTRA - huge)
p("\n" + "=" * 70)
p("ROW COUNTS")
p("=" * 70)
for t in TABLES:
    if t == "ATEXTRA":
        continue
    try:
        cur.execute(f"SELECT COUNT(*) FROM ZJTH.{t}")
        n = cur.fetchone()[0]
        p(f"  {t:15s} {n:>10,} rows")
    except Exception as e:
        p(f"  {t:15s} ERROR {e}")

# 3. Foreign-key constraints declared in DB
p("\n" + "=" * 70)
p("DECLARED FOREIGN KEYS (constraint_type='R')")
p("=" * 70)
try:
    cur.execute(
        """
        SELECT a.TABLE_NAME, c.COLUMN_NAME,
               r.TABLE_NAME AS REF_TABLE, rc.COLUMN_NAME AS REF_COLUMN,
               c.CONSTRAINT_NAME
        FROM ALL_CONSTRAINTS c
        JOIN ALL_CONS_COLUMNS a  ON c.OWNER=a.OWNER AND c.CONSTRAINT_NAME=a.CONSTRAINT_NAME
        JOIN ALL_CONSTRAINTS r   ON c.R_OWNER=r.OWNER AND c.R_CONSTRAINT_NAME=r.CONSTRAINT_NAME
        JOIN ALL_CONS_COLUMNS rc ON r.OWNER=rc.OWNER AND r.CONSTRAINT_NAME=rc.CONSTRAINT_NAME
        WHERE c.OWNER='ZJTH' AND c.CONSTRAINT_TYPE='R'
          AND a.POSITION=rc.POSITION
          AND a.TABLE_NAME IN (:1,:2,:3,:4,:5,:6,:7,:8,:9)
        ORDER BY a.TABLE_NAME, c.CONSTRAINT_NAME, a.POSITION
        """,
        *TABLES[:9],
    )
    rows = cur.fetchall()
    p(f"  ({len(rows)} declared FKs)")
    for r in rows:
        p(f"  {r[0]}.{r[1]}  ->  {r[2]}.{r[3]}   [{r[4]}]")
except Exception as e:
    p(f"  FK QUERY ERROR: {e}")

# 4. Primary keys
p("\n" + "=" * 70)
p("PRIMARY KEYS")
p("=" * 70)
for t in TABLES:
    try:
        cur.execute(
            """
            SELECT cc.COLUMN_NAME
            FROM ALL_CONSTRAINTS c
            JOIN ALL_CONS_COLUMNS cc
              ON c.OWNER=cc.OWNER AND c.CONSTRAINT_NAME=cc.CONSTRAINT_NAME
            WHERE c.OWNER='ZJTH' AND c.TABLE_NAME=:t AND c.CONSTRAINT_TYPE='P'
            ORDER BY cc.POSITION
            """,
            t=t,
        )
        pks = [r[0] for r in cur.fetchall()]
        p(f"  {t:15s} PK = ({', '.join(pks) if pks else 'NONE'})")
    except Exception as e:
        p(f"  {t}: PK ERROR {e}")

out.close()
conn.close()
print("OK — wrote /tmp/oracle_schema_output.txt")
