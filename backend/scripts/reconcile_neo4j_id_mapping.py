"""Neo4j ↔ PG id_mapping 三方对账（v3.1 M0-P0.3）。

读 Neo4j Class/Property/Metric 节点 + PG id_mapping，按 business_object 比对：
- 双侧有 + unified_id 一致 → 通过
- 仅 Neo4j 有 → 回填 PG 占位行（写入 PG，保留 Neo4j 节点）
- 仅 PG 有 → 警告行（不删 PG；需人工判断）
- 双侧有 + unified_id 不一致 → CRITICAL 报错

不动 Neo4j，仅生成 report 与按需补 PG 占位。
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 允许 `python -m scripts.reconcile_neo4j_id_mapping` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neo4j import Driver  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import getSettings  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402


@dataclass(frozen=True)
class ReconcileReport:
    """对账报告（不可变）。

    diff_count：差异总数（= len(rows)，含 placeholder / warning / error）
    exit_code：0=通过 / 1=有错误
    rows：逐条对账明细，供 CLI 打印或前端展示
    placeholder_count：补写入 PG 的占位行数
    warning_count：PG-only 警告数
    error_count：unified_id 不一致错误数
    """

    diff_count: int
    exit_code: int
    rows: list[dict] = field(default_factory=list)
    placeholder_count: int = 0
    warning_count: int = 0
    error_count: int = 0


def _parseBusinessObject(unified_id: str) -> str | None:
    """从 unified_id 解析 business_object：'obj:supplier:S001' → 'supplier'."""
    parts = unified_id.split(":")
    if len(parts) < 3 or parts[0] != "obj":
        return None
    return parts[1]


def _externalIdFromUnifiedId(unified_id: str) -> str:
    """取 unified_id 最后一段作为 external_id：'obj:supplier:S001' → 'S001'."""
    parts = unified_id.split(":")
    return parts[-1] if parts else unified_id


async def reconcile(session: AsyncSession, driver: Driver) -> ReconcileReport:
    """比对 Neo4j Class/Property/Metric 节点 vs PG id_mapping，返回对账报告。

    只读 Neo4j；按需向 PG 写入占位行。commit 在函数末尾统一执行。
    """
    # 1. 读 Neo4j：仅取有 unified_id 的节点（无 uid 的节点无法对账，忽略）
    with driver.session() as ns:
        neo4j_rows = ns.run(
            "MATCH (n) "
            "WHERE (n:Class OR n:Property OR n:Metric) AND n.unified_id IS NOT NULL "
            "RETURN labels(n)[0] AS label, n.unified_id AS uid"
        ).data()

    # 2. 读 PG id_mapping
    pg_rows = (
        await session.execute(
            text("SELECT unified_id, business_object, external_id FROM id_mapping")
        )
    ).mappings().all()

    # 3. 按 business_object 分桶（两端都按 bo 对齐；同 bo 内比对 unified_id 集合）
    pg_by_bo: dict[str, list[dict]] = {}
    for r in pg_rows:
        pg_by_bo.setdefault(r["business_object"], []).append(dict(r))

    neo4j_by_bo: dict[str, list[str]] = {}
    for node in neo4j_rows:
        uid = node.get("uid")
        if not uid:
            continue
        bo = _parseBusinessObject(uid)
        if not bo:
            continue
        neo4j_by_bo.setdefault(bo, []).append(uid)

    rows: list[dict] = []
    placeholder_count = 0
    warning_count = 0
    error_count = 0

    all_bos = set(pg_by_bo.keys()) | set(neo4j_by_bo.keys())
    for bo in all_bos:
        pg_entries = pg_by_bo.get(bo, [])
        neo4j_uids = neo4j_by_bo.get(bo, [])

        if pg_entries and neo4j_uids:
            # 双侧都有：比对 unified_id 集合
            pg_uids = {r["unified_id"] for r in pg_entries}
            n4j_uids = set(neo4j_uids)
            if pg_uids != n4j_uids:
                rows.append(
                    {
                        "kind": "unified_id_mismatch",
                        "business_object": bo,
                        "pg_unified_ids": sorted(pg_uids),
                        "neo4j_unified_ids": sorted(n4j_uids),
                    }
                )
                error_count += 1
        elif neo4j_uids:
            # 仅 Neo4j：写 PG 占位（每行用 savepoint 隔离失败，避免一行报错
            # 污染整 session 的 INSERT 序列；异常时 rollback 到 savepoint，前
            # 面的成功行仍会随函数末尾 commit 落库）
            for uid in neo4j_uids:
                sp = await session.begin_nested()
                try:
                    await session.execute(
                        text(
                            "INSERT INTO id_mapping "
                            "(unified_id, business_object, external_id, "
                            "created_time, updated_time) "
                            "VALUES (:uid, :bo, :ext, now(), now()) "
                            "ON CONFLICT (business_object, external_id) DO NOTHING"
                        ),
                        {
                            "uid": uid,
                            "bo": bo,
                            "ext": _externalIdFromUnifiedId(uid),
                        },
                    )
                    await sp.commit()
                except IntegrityError as exc:
                    await sp.rollback()
                    rows.append(
                        {
                            "kind": "placeholder_failed",
                            "business_object": bo,
                            "unified_id": uid,
                            "error": str(exc.orig)[:200],
                        }
                    )
                    continue
                rows.append(
                    {
                        "kind": "placeholder_written",
                        "business_object": bo,
                        "unified_id": uid,
                    }
                )
                placeholder_count += 1
        else:
            # 仅 PG：警告（不删）
            for r in pg_entries:
                rows.append(
                    {
                        "kind": "pg_only_warning",
                        "business_object": bo,
                        "unified_id": r["unified_id"],
                    }
                )
                warning_count += 1

    await session.commit()

    diff_count = len(rows)
    exit_code = 1 if error_count > 0 else 0

    return ReconcileReport(
        diff_count=diff_count,
        exit_code=exit_code,
        rows=rows,
        placeholder_count=placeholder_count,
        warning_count=warning_count,
        error_count=error_count,
    )


async def _main() -> int:
    """CLI 入口：连真实 PG + Neo4j 跑全量对账，返回 exit_code。"""
    factory = getSessionFactory()
    settings = getSettings()
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.neo4jUri,
        auth=(settings.neo4jUser, settings.neo4jPassword),
    )
    try:
        async with factory() as session:
            report = await reconcile(session, driver)
        print(f"diff_count={report.diff_count}")
        print(
            f"placeholder={report.placeholder_count} "
            f"warning={report.warning_count} "
            f"error={report.error_count}"
        )
        for row in report.rows:
            print(row)
        return report.exit_code
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))