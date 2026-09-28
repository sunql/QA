"""M0-P0.3: 回填 Neo4j Class/Property/Metric 节点的 unified_id (= external_id)。

流程（按 batch_size 分批，事务边界在 PG）：
  Step 1: 对无 unified_id 的节点 → 生成 obj:{Label}:{legacy_id}
          → INSERT PG + SET Neo4j
  Step 2: 对已有 unified_id 的节点 → 仅 INSERT PG（确保 PG↔Neo4j 对齐）

幂等：第二次跑无新写入（所有节点已对齐，PG ON CONFLICT DO NOTHING 生效）。

CQL 安全：label 来自 _ALLOWED_LABELS 白名单（frozenset({Class, Property, Metric}），
模板查表而非 f-string 拼接，避免任何注入路径（即使白名单被绕过）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 允许 `python -m scripts.backfill_neo4j_external_id` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neo4j import Driver  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.config import getSettings  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.infrastructure.neo4j_client import _ALLOWED_LABELS  # noqa: E402


# 预编译 CQL 模板（label 查表，不走 f-string；双重防护 CQL 注入）
_SET_UNIFIED_ID_CQL = {
    "Class": "MATCH (n:Class {id: $legacy_id}) SET n.unified_id = $unified_id",
    "Property": "MATCH (n:Property {id: $legacy_id}) SET n.unified_id = $unified_id",
    "Metric": "MATCH (n:Metric {id: $legacy_id}) SET n.unified_id = $unified_id",
}


def _parseBusinessObject(unified_id: str) -> str | None:
    """从 unified_id 解析 business_object：'obj:Class:100' → 'Class'."""
    parts = unified_id.split(":")
    if len(parts) < 3 or parts[0] != "obj":
        return None
    return parts[1]


def _externalIdFromUnifiedId(unified_id: str) -> str:
    """取 unified_id 最后一段作为 external_id."""
    parts = unified_id.split(":")
    return parts[-1] if parts else unified_id


async def backfill(session: AsyncSession, driver: Driver, batch_size: int = 500) -> int:
    """回填 Neo4j Class/Property/Metric 节点的 unified_id + PG id_mapping 占位。

    - 无 unified_id 的节点：生成 obj:{Label}:{legacy_id}，INSERT PG + SET Neo4j
    - 已有 unified_id 的节点：仅 INSERT PG（确保 PG↔Neo4j 对齐）

    返回值 written = 新生成 unified_id 的节点数（即原本无 unified_id 的节点数）。
    """
    written = 0

    # Step 1: 处理无 unified_id 的节点（按 batch_size 分批，避免单事务过长）
    while True:
        with driver.session() as ns:
            batch = ns.run(
                "MATCH (n) WHERE (n:Class OR n:Property OR n:Metric) "
                "AND n.unified_id IS NULL "
                "WITH n LIMIT $limit "
                "RETURN labels(n)[0] AS label, n.id AS legacy_id",
                limit=batch_size,
            ).data()
        if not batch:
            break

        for node in batch:
            label = node["label"]
            legacy_id = node["legacy_id"]
            if legacy_id is None:
                # 无 id 属性的节点无法生成 unified_id，跳过
                continue
            if label not in _ALLOWED_LABELS:
                # 防御性校验：即使上游异常，label 必须在白名单内
                raise ValueError(f"Invalid label: {label!r}")
            legacy_id_str = str(legacy_id)
            unified_id = f"obj:{label}:{legacy_id_str}"

            # INSERT PG（savepoint 隔离 IntegrityError，避免单行失败污染整 session）
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
                    {"uid": unified_id, "bo": label, "ext": legacy_id_str},
                )
                await sp.commit()
            except IntegrityError:
                await sp.rollback()
                # PG 已存在同 (bo, ext) 行，视为已写入；Neo4j SET 仍需执行以对齐

            # SET Neo4j unified_id（查表模板，避免 f-string 注入）
            cql = _SET_UNIFIED_ID_CQL[label]
            with driver.session() as ns2:
                ns2.run(cql, legacy_id=legacy_id, unified_id=unified_id)

        await session.commit()
        written += len(batch)

    # Step 2: 为已有 unified_id 的节点写 PG 占位（确保 PG↔Neo4j 对齐）
    with driver.session() as ns:
        aligned = ns.run(
            "MATCH (n) WHERE (n:Class OR n:Property OR n:Metric) "
            "AND n.unified_id IS NOT NULL "
            "RETURN labels(n)[0] AS label, n.unified_id AS uid"
        ).data()

    for node in aligned:
        uid = node["uid"]
        bo = _parseBusinessObject(uid)
        if not bo:
            continue
        ext = _externalIdFromUnifiedId(uid)
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
                {"uid": uid, "bo": bo, "ext": ext},
            )
            await sp.commit()
        except IntegrityError:
            await sp.rollback()
    await session.commit()

    return written


async def _main() -> int:
    """CLI 入口：连真实 PG + Neo4j 跑全量回填，返回 0。"""
    factory = getSessionFactory()
    settings = getSettings()
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        settings.neo4jUri,
        auth=(settings.neo4jUser, settings.neo4jPassword),
    )
    try:
        async with factory() as session:
            written = await backfill(session, driver)
        print(f"written={written}")
        return 0
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))