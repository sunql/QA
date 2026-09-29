"""Milvus ↔ PG id_mapping 三方对账（v3.1 M0-P0.4）。

读 3 个新本体 Milvus 集合（class/property/metric）+ PG id_mapping，按
business_object 比对：
- 双侧有 + unified_id 一致 → 通过
- 仅 Milvus 有 → 回填 PG 占位行（写入 PG，保留 Milvus 行）
- 仅 PG 有 → 警告行（不删 PG；需人工判断）
- 双侧有 + unified_id 不一致 → CRITICAL 报错

不动 Milvus，仅生成 report 与按需补 PG 占位。
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 允许 `python -m scripts.reconcile_milvus_id_mapping` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.infrastructure.milvus_client import (  # noqa: E402
    _CLASS_COLLECTION_NAME,
    _METRIC_COLLECTION_NAME,
    _PROPERTY_COLLECTION_NAME,
    _connect,
    queryClassEmbeddings,
    queryMetricEmbeddings,
    queryPropertyEmbeddings,
)

_TYPE_TO_COLLECTION: dict[str, str] = {
    "class": _CLASS_COLLECTION_NAME,
    "property": _PROPERTY_COLLECTION_NAME,
    "metric": _METRIC_COLLECTION_NAME,
}


@dataclass(frozen=True)
class ReconcileReport:
    """对账报告（不可变）。字段语义与 Neo4j reconcile 一致。"""

    diff_count: int
    exit_code: int
    rows: list[dict] = field(default_factory=list)
    placeholder_count: int = 0
    warning_count: int = 0
    error_count: int = 0


def _expectedUnifiedId(type_: str, ontology_id: int) -> str:
    """Generate expected unified_id from Milvus type + ontology_id."""
    return f"obj:{type_}:{ontology_id}"


def _parseBusinessObject(unified_id: str) -> str | None:
    """'obj:supplier:S001' → 'supplier'."""
    parts = unified_id.split(":")
    if len(parts) < 3 or parts[0] != "obj":
        return None
    return parts[1]


def _externalIdFromUnifiedId(unified_id: str) -> str:
    """'obj:supplier:S001' → 'S001'."""
    parts = unified_id.split(":")
    return parts[-1] if parts else unified_id


async def reconcile(session: AsyncSession) -> ReconcileReport:
    """比对 3 个新 Milvus 集合 vs PG id_mapping，返回对账报告。

    只读 Milvus；按需向 PG 写入占位行。commit 在函数末尾统一执行。
    """
    # 1. 读 3 个新 Milvus 集合
    _connect()
    milvus_rows: list[dict] = []
    for type_, query_fn in (
        ("class", queryClassEmbeddings),
        ("property", queryPropertyEmbeddings),
        ("metric", queryMetricEmbeddings),
    ):
        for r in query_fn():
            milvus_rows.append({
                "type": type_,
                "ontology_id": int(r["ontology_id"]),
                "milvus_collection": _TYPE_TO_COLLECTION[type_],
            })

    # 2. 读 PG id_mapping（仅看 milvus_collection 不为空的行；其它引擎的对账不在本次范围）
    pg_rows = (
        await session.execute(
            text(
                "SELECT unified_id, business_object, external_id, milvus_collection "
                "FROM id_mapping WHERE milvus_collection IS NOT NULL"
            )
        )
    ).mappings().all()

    # 3. 双向对账
    rows: list[dict] = []
    placeholder_count = 0
    warning_count = 0
    error_count = 0

    # 按 (business_object, external_id) 分桶
    pg_by_key: dict[tuple[str, str], dict] = {}
    for r in pg_rows:
        pg_by_key[(r["business_object"], r["external_id"])] = dict(r)

    milvus_seen_keys: set[tuple[str, str]] = set()
    for m in milvus_rows:
        key = (m["type"], str(m["ontology_id"]))
        milvus_seen_keys.add(key)
        expected_uid = _expectedUnifiedId(m["type"], m["ontology_id"])
        pg_entry = pg_by_key.get(key)
        if pg_entry is None:
            # 仅 Milvus：写 PG 占位
            sp = await session.begin_nested()
            try:
                await session.execute(
                    text(
                        "INSERT INTO id_mapping "
                        "(unified_id, business_object, external_id, milvus_collection, "
                        "milvus_id, created_time, updated_time) "
                        "VALUES (:uid, :bo, :ext, :mc, :mid, now(), now()) "
                        "ON CONFLICT (business_object, external_id) DO NOTHING"
                    ),
                    {
                        "uid": expected_uid,
                        "bo": m["type"],
                        "ext": str(m["ontology_id"]),
                        "mc": m["milvus_collection"],
                        "mid": str(m["ontology_id"]),
                    },
                )
                await sp.commit()
                rows.append({
                    "kind": "placeholder_written",
                    "business_object": m["type"],
                    "unified_id": expected_uid,
                })
                placeholder_count += 1
            except IntegrityError as exc:
                await sp.rollback()
                rows.append({
                    "kind": "placeholder_failed",
                    "business_object": m["type"],
                    "unified_id": expected_uid,
                    "error": str(exc.orig)[:200],
                })
        elif pg_entry["unified_id"] != expected_uid:
            # 双侧都有但 unified_id 不一致
            rows.append({
                "kind": "unified_id_mismatch",
                "business_object": m["type"],
                "pg_unified_id": pg_entry["unified_id"],
                "milvus_unified_id": expected_uid,
            })
            error_count += 1
        # else: 双侧一致 → 通过（不写入 rows）

    # PG → Milvus：检查 PG 行是否都有对应 Milvus 行
    for r in pg_rows:
        key = (r["business_object"], r["external_id"])
        if key not in milvus_seen_keys:
            rows.append({
                "kind": "pg_only_warning",
                "business_object": r["business_object"],
                "unified_id": r["unified_id"],
            })
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
    """CLI 入口：连真实 PG + Milvus 跑全量对账，返回 exit_code。"""
    factory = getSessionFactory()

    async with factory() as session:
        report = await reconcile(session)
    print(f"diff_count={report.diff_count}")
    print(
        f"placeholder={report.placeholder_count} "
        f"warning={report.warning_count} "
        f"error={report.error_count}"
    )
    for row in report.rows:
        sanitized = {k: v for k, v in row.items() if k != "error"}
        print(sanitized)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
