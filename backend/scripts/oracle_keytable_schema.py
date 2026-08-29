"""查询关键表.md 中未建模的表，确认实际表名并导出列结构。

候选表（来自 关键表.md）：
  PREQUISD    采购需求明细
  PREQUISO    采购请求头（与采购请求关联）
  PPRICFICH   供应商价格（记录）
  PPRICLIST   供应商价格明细
"""
import os

import oracledb

DSN = "192.168.205.70:1521/X3V71ORA"
USER = "ZJTH"
# 口令不硬编码：经环境变量注入（可由应用 SECRET_KEY 解密 data_source.password_encrypted 取得）
PWD = os.environ.get("ORACLE_PASSWORD")
if not PWD:
    raise SystemExit("缺少 ORACLE_PASSWORD 环境变量（ZJTH 业务库口令，勿硬编码于脚本）")

conn = oracledb.connect(user=USER, password=PWD, dsn=DSN)
cur = conn.cursor()

# 1) 先模糊查找可能的表名，确认实际存在
cur.execute(
    """
    SELECT TABLE_NAME FROM ALL_TABLES
    WHERE OWNER='ZJTH'
      AND (TABLE_NAME LIKE 'PREQUIS%'
        OR TABLE_NAME LIKE 'PORDER%'
        OR TABLE_NAME LIKE 'PPRIC%'
        OR TABLE_NAME LIKE 'PORDERH%'
        OR TABLE_NAME LIKE 'PINVOICE%')
    ORDER BY TABLE_NAME
    """
)
found = [r[0] for r in cur.fetchall()]
print("=== matched tables ===")
for t in found:
    print(" ", t)

# 2) 导出候选表的列结构 + 行数
CANDIDATES = ["PREQUISD", "PREQUISO", "PPRICFICH", "PPRICLIST"]
out = open("/tmp/keytable_schema.txt", "w", encoding="utf-8")
for t in CANDIDATES:
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
    if not rows:
        print(f"\n### {t}  (NOT FOUND)", file=out)
        continue
    print(f"\n### {t}  ({len(rows)} columns)", file=out)
    print("-" * 60, file=out)
    for cn, dt, dl, nl in rows:
        print(f"  {cn:28s} {dt:18s} ({dl:>5}) {'NULL' if nl=='Y' else 'NOT NULL'}", file=out)
    try:
        cur.execute(f"SELECT COUNT(*) FROM ZJTH.{t}")
        n = cur.fetchone()[0]
        print(f"  >>> rows: {n:,}", file=out)
    except Exception as e:
        print(f"  >>> count error: {e}", file=out)

out.close()
conn.close()
print("OK -> /tmp/keytable_schema.txt")
