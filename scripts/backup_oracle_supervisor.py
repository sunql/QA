#!/usr/bin/env python3
"""THBI Oracle 备份的**断点续跑看门狗**（配合 scripts/backup_oracle_schema.py 用）。

为什么需要它：手工起的长跑备份进程会被外部因素打断（实测 2026-11-28 00:16 进程
凭空消失，文件停在一张表的 DDL 上、没写数据；无 OOM、无崩溃报告）。
本脚本把「算缺失表 → 只补缺失的 → 再算，直到补齐」做成循环，
子进程用 `start_new_session=True` **脱离本会话进程组**，不被连带杀掉。

⚠️ **不能用 `--skip-tables` 做续传**：`--skip-tables` 只能跳**前缀**，
一旦某轮不是从第 1 张表开始（或 skip 逻辑写重了），中间的表就会**整段漏掉**。
⇒ 本脚本改为**按「缺失集合」补**（`--only-tables`），与前缀假设无关。

完成判定（逐文件）：
- 文件以 `COMMIT;` 结尾 ⇒ 该文件所有表都完整；
- 否则，**带备注行**（`-- 时间过滤：` / `-- ⚠️ …`）的表完整，最后一张不完整。

用法（在 backend/ 目录下）：
    python ../scripts/backup_oracle_supervisor.py --datasource-id 1 --schema THBI \
        --from-year 2025 --out ../backups/oracle
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(REPO / "scripts"))

HEADER_RE = re.compile(rb"^-- ===== THw?BI\.(\w+) =====$")
NOTE_PREFIXES = ("-- 时间过滤：", "-- ⚠️ ")

#: **旧规则**（2026-11-27 之前的实现）——只用于识别「哪些表需要用新规则重导」。
_OLD_BUSINESS_DATE_COLUMNS = (
    "ORDER_DATE", "EXP_RCV_DATE", "EXPECTED_DATE", "EXPECT_RECEIPT_DATE",
    "RECEIPT_DATE", "ACTUAL_RECEIPT_DATE", "ARR_DATE", "ARRIVAL_DATE",
    "DELIVERY_DATE", "INVOICE_DATE", "BILL_DATE", "DOC_DATE", "PLAN_DATE",
    "CREATION_DATE", "CREATED_DATE", "CREATION_TIME", "CREATED_TIME",
)


def oldRuleFilterColumn(tableName: str, columns: list[dict]) -> str | None:
    """旧实现的过滤列判定（用于识别「规则变了 ⇒ 需要重导」的表）。"""
    byName = {c["name"].upper(): c for c in columns}
    for candidate in _OLD_BUSINESS_DATE_COLUMNS:
        column = byName.get(candidate)
        if column is not None:
            return column["name"]
    for column in columns:
        if str(column["type"]).upper().startswith(("DATE", "TIMESTAMP")):
            return column["name"]
    ymonth = byName.get("YMONTH")
    if ymonth is not None:
        return ymonth["name"]
    return None


def completeTables(path: pathlib.Path, owner: str) -> set[str]:
    """该文件里**数据完整**的表名集合。

    - 文件以 `COMMIT;` 结尾 ⇒ 所有表完整；
    - 否则，**带备注行**的表完整（备注行写在该表 INSERT 之后 ⇒ 有备注 = 写完了）。
    """
    names: list[str] = []
    noted: set[str] = set()
    current: str | None = None
    tail = b""
    remainder = b""
    pattern = re.compile(rb"^-- ===== " + owner.encode() + rb"\.(\w+) =====$")
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 22)
            if not chunk:
                break
            tail = chunk[-64:]
            remainder += chunk
            lines = remainder.split(b"\n")
            remainder = lines.pop()
            for line in lines:
                matched = pattern.match(line)
                if matched:
                    current = matched.group(1).decode()
                    names.append(current)
                elif current and line.decode("utf-8", "replace").startswith(NOTE_PREFIXES):
                    noted.add(current)
    if not names:
        return set()
    if b"COMMIT;" in tail:
        return set(names)
    return set(names) & noted


def log(message: str, logPath: pathlib.Path) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    with logPath.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


async def canonicalOrder(args: argparse.Namespace) -> list[dict]:
    """按「导出顺序」读出全部表（与 backup_oracle_schema.py 内部一致：Python 排序）。"""
    import backup_oracle_schema as bos
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
    connection = await oracledb_connect(ds, password)
    try:
        cursor = connection.cursor()
        tables = await bos._columnsOf(cursor, args.schema.upper())
        cursor.close()
    finally:
        await connection.close()
    return tables


async def oracledb_connect(ds, password):
    import oracledb

    dsn = f"{ds.host}:{ds.port}/{ds.database_name}"
    return await oracledb.connect_async(user=ds.username, password=password, dsn=dsn)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Oracle 备份断点续跑看门狗")
    parser.add_argument("--datasource-id", type=int, default=1)
    parser.add_argument("--schema", default="THBI")
    parser.add_argument("--from-year", type=int, default=2025)
    parser.add_argument("--out", default="../backups/oracle")
    parser.add_argument("--max-rounds", type=int, default=30)
    args = parser.parse_args()

    import backup_oracle_schema as bos

    outDir = pathlib.Path(args.out).resolve()
    outDir.mkdir(parents=True, exist_ok=True)
    logPath = outDir / "backup_supervisor.log"
    owner = args.schema.upper()

    tables = await canonicalOrder(args)
    order = [t["name"] for t in tables]
    changed = {
        t["name"]
        for t in tables
        if oldRuleFilterColumn(t["name"], t["columns"])
        != bos._pickFilterColumn(t["name"], t["columns"])[0]
    }
    total = len(order)
    log(f"看门狗启动：目标 {total} 张表；规则已变的表 {len(changed)} 张", logPath)

    for attempt in range(1, args.max_rounds + 1):
        newDone: set[str] = set()
        oldDone: set[str] = set()
        for path in sorted(outDir.glob(f"{owner}_schema_*.sql")):
            complete = completeTables(path, owner)
            if not complete:
                continue
            # 「旧规则产出」= 备注行里写着旧规则用过的技术时间戳列名 ⇒ 保守起见都算旧
            if path.name in _OLD_RULE_FILES:
                oldDone |= complete
            else:
                newDone |= complete
        covered = newDone | oldDone
        missing = [t for t in order if t not in covered]
        # 旧规则导出的表里，**过滤列变了**的那些内容是错的 ⇒ 必须用新规则重导
        stale = [t for t in order if t in oldDone and t in changed and t not in newDone]
        pending = sorted(set(missing) | set(stale), key=order.index)
        if not pending:
            log(f"✅ 全部 {total} 张表已用新规则导出", logPath)
            return 0
        log(
            f"第 {attempt} 轮：新规则已导出 {len(newDone)}，缺 {len(missing)}，"
            f"需重导 {len(stale)} ⇒ 本轮 {len(pending)} 张",
            logPath,
        )
        result = subprocess.run(
            [
                "uv", "run", "--frozen", "--extra", "dev", "python",
                "../scripts/backup_oracle_schema.py",
                "--datasource-id", str(args.datasource_id),
                "--schema", args.schema,
                "--from-year", str(args.from_year),
                "--only-tables", ",".join(pending),
                "--out", args.out,
            ],
            cwd=BACKEND,
            capture_output=True,
            text=True,
        )
        tail = (result.stdout or "").strip().splitlines()[-3:]
        log(f"第 {attempt} 轮退出码={result.returncode} 尾部：{tail}", logPath)
        if result.stderr:
            log(f"stderr：{result.stderr.strip()[-300:]}", logPath)
        time.sleep(3)
    log(f"❌ {args.max_rounds} 轮仍未完成，人工介入", logPath)
    return 1


#: 已知用**旧规则**产出的分片文件名（第一版实现把 CREATION_DATE 等技术时间戳当业务日期，
#: 且没有「过滤后 0 行 ⇒ 回退全量」的兜底 ⇒ 这些分片里的表内容不可信）。
_OLD_RULE_FILES = frozenset(
    {
        "THBI_schema_20261127_1503.sql",
        "THBI_schema_20261127_1556.sql",
    }
)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
