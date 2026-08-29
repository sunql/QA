"""查询采购收货 4 张表的列结构，补充本体建模依据。

表：
  YPRECEIPT   到货单表
  YPRECEIPTD  到货明细表
  PRECEIPT    收货单表
  PRECEIPTD   收货单明细表
"""
import os

import oracledb

DSN = "192.168.205.70:1521/X3V71ORA"
USER = "ZJTH"
# 口令不硬编码：经环境变量注入（可由应用 SECRET_KEY 解密 data_source.password_encrypted 取得）
PWD = os.environ.get("ORACLE_PASSWORD")
if not PWD:
    raise SystemExit("缺少 ORACLE_PASSWORD 环境变量（ZJTH 业务库口令，勿硬编码于脚本）")

TABLES = ["YPRECEIPT", "YPRECEIPTD", "PRECEIPT", "PRECEIPTD"]

conn = oracledb.connect(user=USER, password=PWD, dsn=DSN)
cur = conn.cursor()

out = open("/tmp/receipt_schema.txt", "w", encoding="utf-8")
for t in TABLES:
    cur.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE
        FROM ALL_TAB_COLUMNS
        WHERE OWNER='ZJTH' AND TABLE_NAME=:t
        ORDER BY COLUMN_ID
        """,
        t=t,
    )
    rows = cur.fetchall()
    print(f"\n### {t}  ({len(rows)} columns)", file=out)
    print("-" * 60, file=out)
    for cn, dt, dl, nl in rows:
        print(f"  {cn:28s} {dt:18s} ({dl:>5}) {'NULL' if nl=='Y' else 'NOT NULL'}", file=out)
    # 行数
    cur.execute(f"SELECT COUNT(*) FROM ZJTH.{t}")
    n = cur.fetchone()[0]
    print(f"  >>> rows: {n:,}", file=out)

out.close()
conn.close()
print("OK -> /tmp/receipt_schema.txt")
