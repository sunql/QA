"""P3a 复核：**独立**交叉核对 v2 行的治理维度与 PG 治理事实。

为什么必须有这个脚本
--------------------
D20 的影子比对里，「哪些行属于该范围」的**真值**是从 **v2 行自己带的维度字段**
读出来的 ⇒ **自指**：如果回填把维度写错了（域码写成别的域、组织码写成别的组织），
过滤与真值会**一起错** ⇒ 「泄漏 0」照样成立 ⇒ **假绿**。

本脚本用**另一条代码路径**（直接 SQL，见 ``_EXPECTED_DIMENSIONS_SQL``）从 PG
治理表重新推导期望维度，再与 v2 行里**实际存的值**逐行比对：

- 只有「PG 治理事实」与「v2 存量」一致，D20 的「泄漏 0」才说明
  「过滤按治理事实生效」，而不只是「按自己写进去的值自洽」。

⚠️ 口径说明
------------
- **不比对** ``release_id``：本仓还没有「语义发布 → 向量行」的接线，
  v2 里恒为 0（= 无激活发布），比对无意义；
- 「object_owner 是否算组织码」在 SQL 里用 ``EXISTS`` 判定（**精确匹配**
  组织码），与「匹配不到就不并入」的口径一致；
- epoch 毫秒的换算在本脚本里**独立实现**（naive 视作 UTC），
  不 import ``ontology_dimension_service``（避免又变成同一条路径）。

用法
----
    DATABASE_URL=postgresql+asyncpg://...@localhost:5433/qa_metadata \
    MILVUS_URI=http://localhost:19530 \
        uv run python scripts/verify_registry_v2_dimensions.py \
            --target-db qa_registry_v2

退出码：0 = 全部一致；1 = 有不一致（逐条打印）；2 = 参数/环境问题。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

from pymilvus import MilvusClient

from app.infrastructure.milvus_registry_v2 import REGISTRY_V2_COLLECTION_NAMES

ALL_TYPES: tuple[str, ...] = ("class", "property", "metric", "kpi")

# 单页读取行数
_READ_PAGE = 1000

# 独立 SQL：从治理表直接推导期望维度（**不经过** ontology_dimension_service）。
_EXPECTED_DIMENSIONS_SQL = """
WITH class_dim AS (
  SELECT c.id AS class_id,
         COALESCE(bo.code, '') AS entity_type,
         COALESCE(dd.code, '') AS primary_domain,
         COALESCE((
             SELECT string_agg(ad.code, ',' ORDER BY ad.code)
             FROM business_object_domain bod
             JOIN data_domain ad ON ad.code = bod.domain_id
             WHERE bod.bo_code = bo.code
         ), '') AS attached_domains,
         COALESCE((
             SELECT o.code FROM organizations o
             JOIN data_domain d2 ON d2.primary_org_id = o.id
             WHERE d2.code = bo.primary_domain_id
         ), '') AS domain_org,
         CASE WHEN EXISTS (SELECT 1 FROM organizations o2 WHERE o2.code = c.object_owner)
              THEN c.object_owner ELSE '' END AS owner_org,
         c.valid_from,
         c.valid_to
  FROM ontology_class c
  LEFT JOIN business_object bo ON bo.header_class_id = c.id
  LEFT JOIN data_domain dd ON dd.code = bo.primary_domain_id
)
SELECT 'class' AS dim_type, class_id AS ontology_id, entity_type, primary_domain,
       attached_domains, domain_org, owner_org, valid_from, valid_to
  FROM class_dim
UNION ALL
SELECT 'property', p.id,
       COALESCE(cd.entity_type, ''), COALESCE(cd.primary_domain, ''),
       COALESCE(cd.attached_domains, ''), COALESCE(cd.domain_org, ''),
       COALESCE(cd.owner_org, ''),
       COALESCE(cd.valid_from, TIMESTAMP '1970-01-01 00:00:00+00'),
       COALESCE(cd.valid_to, TIMESTAMP '1970-01-01 00:00:00+00')
  FROM ontology_property p LEFT JOIN class_dim cd ON cd.class_id = p.class_id
UNION ALL
SELECT 'metric', m.id,
       COALESCE(cd.entity_type, ''), COALESCE(cd.primary_domain, ''),
       COALESCE(cd.attached_domains, ''), COALESCE(cd.domain_org, ''),
       COALESCE(cd.owner_org, ''),
       COALESCE(cd.valid_from, TIMESTAMP '1970-01-01 00:00:00+00'),
       COALESCE(cd.valid_to, TIMESTAMP '1970-01-01 00:00:00+00')
  FROM ontology_metric m LEFT JOIN class_dim cd ON cd.class_id = m.target_class_id
UNION ALL
SELECT 'kpi', k.id,
       COALESCE(cd.entity_type, ''), COALESCE(cd.primary_domain, ''),
       COALESCE(cd.attached_domains, ''), COALESCE(cd.domain_org, ''),
       COALESCE(cd.owner_org, ''),
       COALESCE(cd.valid_from, TIMESTAMP '1970-01-01 00:00:00+00'),
       COALESCE(cd.valid_to, TIMESTAMP '1970-01-01 00:00:00+00')
  FROM kpi_catalog k
  LEFT JOIN ontology_metric m ON m.id = k.metric_id
  LEFT JOIN class_dim cd ON cd.class_id = m.target_class_id
"""

# 比对的维度字段（release_id 不在其中：本仓尚无「发布 → 向量行」接线）
_COMPARED_FIELDS: tuple[str, ...] = (
    "domain_codes",
    "org_codes",
    "entity_type",
    "valid_from",
    "valid_to",
)


# ---------------------------------------------------------------------------
# 纯逻辑（单测覆盖，不连 Milvus / PG）
# ---------------------------------------------------------------------------


def _epochMs(value: Any) -> int:
    """datetime → epoch 毫秒；``None``/空 ⇒ 0（哨兵）。

    naive datetime 视作 UTC（与 PG 侧 ``timestamp with time zone`` 口径一致）。
    """
    if value is None:
        return 0
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return int(value.timestamp() * 1000)
    return int(value)


def expectedDimension(row: dict[str, Any]) -> dict[str, Any]:
    """SQL 行 → **期望维度**（纯函数）。

    Args:
        row: ``_EXPECTED_DIMENSIONS_SQL`` 的一行（键名与 SQL 列名一致）。

    Returns:
        ``{维度字段: 值}``；域码/组织码是**排序去重**后的元组（与写入口径一致）。
    """
    primary = str(row.get("primary_domain") or "").strip()
    attached = tuple(
        sorted({c for c in str(row.get("attached_domains") or "").split(",") if c})
    )
    domainCodes = tuple(sorted({primary, *attached} - {""}))
    orgCodes = tuple(
        sorted(
            {
                c
                for c in (
                    str(row.get("domain_org") or "").strip(),
                    str(row.get("owner_org") or "").strip(),
                )
                if c
            }
        )
    )
    return {
        "domain_codes": domainCodes,
        "org_codes": orgCodes,
        "entity_type": str(row.get("entity_type") or "").strip(),
        "valid_from": _epochMs(row.get("valid_from")),
        "valid_to": _epochMs(row.get("valid_to")),
    }


def _actualDimension(row: dict[str, Any]) -> dict[str, Any]:
    """v2 行 → 实际维度（纯函数）。

    数组字段按**集合**语义比对（排序后比较）：写入口径是「排序去重」，
    顺序差异不是差异 ⇒ 免得把「同一组码、顺序不同」误报成不一致。
    """
    return {
        "domain_codes": tuple(sorted(str(c) for c in (row.get("domain_codes") or ()))),
        "org_codes": tuple(sorted(str(c) for c in (row.get("org_codes") or ()))),
        "entity_type": str(row.get("entity_type") or ""),
        "valid_from": int(row.get("valid_from") or 0),
        "valid_to": int(row.get("valid_to") or 0),
    }


def compareDimensions(
    v2Rows: list[dict[str, Any]],
    expectedRows: list[dict[str, Any]],
) -> dict[str, Any]:
    """逐行比对 v2 存量维度与 PG 期望维度。

    Args:
        v2Rows: v2 集合读出的行（每行含 ``type`` / ``ontology_id`` 与维度字段）。
        expectedRows: PG 侧期望行（每行含 ``dim_type`` / ``ontology_id`` 与期望字段）。

    Returns:
        ``{compared, matched, mismatched, orphanRows, missingInV2, mismatches}``：
        - ``mismatches``：``[{type, ontology_id, field, expected, actual}]``；
        - ``orphanRows``：v2 里有、PG 治理表里已无对应实体（数据漂移，不算维度错）；
        - ``missingInV2``：PG 有实体但 v2 没有向量行（正常：不是每个实体都有向量）。
    """
    expectedByKey: dict[tuple[str, int], dict[str, Any]] = {}
    for row in expectedRows:
        key = (str(row.get("dim_type") or ""), int(row.get("ontology_id") or 0))
        expectedByKey[key] = expectedDimension(row)

    mismatches: list[dict[str, Any]] = []
    orphanRows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for row in v2Rows:
        key = (str(row.get("type") or ""), int(row.get("ontology_id") or 0))
        seen.add(key)
        expected = expectedByKey.get(key)
        if expected is None:
            orphanRows.append({"type": key[0], "ontology_id": key[1]})
            continue
        actual = _actualDimension(row)
        for field in _COMPARED_FIELDS:
            if expected[field] != actual[field]:
                mismatches.append(
                    {
                        "type": key[0],
                        "ontology_id": key[1],
                        "field": field,
                        "expected": expected[field],
                        "actual": actual[field],
                    }
                )

    missingInV2 = [
        {"type": type_, "ontology_id": ontologyId}
        for (type_, ontologyId) in expectedByKey
        if (type_, ontologyId) not in seen
    ]

    compared = len(seen) - len(orphanRows)
    mismatchedKeys = {m["ontology_id"] for m in mismatches}
    return {
        "v2Rows": len(v2Rows),
        "compared": compared,
        "matched": compared - len(mismatchedKeys),
        "mismatched": len(mismatchedKeys),
        "orphanRows": orphanRows,
        "missingInV2": missingInV2,
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# 真实连接（PG + Milvus）
# ---------------------------------------------------------------------------


async def _readExpectedDimensions() -> list[dict[str, Any]]:
    """从 PG 治理表读出期望维度（独立 SQL 路径）。"""
    from sqlalchemy import text

    from app.infrastructure.database import getSessionFactory

    async with getSessionFactory()() as session:
        result = await session.execute(text(_EXPECTED_DIMENSIONS_SQL))
        return [dict(row._mapping) for row in result]


def _readV2Rows(client: MilvusClient, type_: str) -> list[dict[str, Any]]:
    """读 v2 集合里的维度字段（**不含向量**，只要治理维度）。"""
    name = REGISTRY_V2_COLLECTION_NAMES[type_]
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.query(
            name,
            filter="id >= 0",
            output_fields=[
                "ontology_id",
                "type",
                "domain_codes",
                "org_codes",
                "entity_type",
                "valid_from",
                "valid_to",
            ],
            limit=_READ_PAGE,
            offset=offset,
        )
        if not page:
            break
        rows.extend(page)
        offset += len(page)
        if len(page) < _READ_PAGE:
            break
    return rows


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """执行核对并返回报告。"""
    uri = args.milvus_uri or os.environ.get("MILVUS_URI", "http://localhost:19530")
    client = MilvusClient(uri=uri, db_name=args.target_db)
    try:
        v2Rows: list[dict[str, Any]] = []
        for type_ in ALL_TYPES:
            v2Rows.extend(_readV2Rows(client, type_))
    finally:
        client.close()

    expectedRows = await _readExpectedDimensions()
    report = compareDimensions(v2Rows, expectedRows)
    report["targetDb"] = args.target_db
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对 v2 集合的治理维度与 PG 治理事实")
    parser.add_argument("--target-db", default="qa_registry_v2", help="v2 集合所在 Milvus 库")
    parser.add_argument("--milvus-uri", default=None, help="Milvus URI（缺省取 MILVUS_URI）")
    parser.add_argument("--report-json", default=None, help="把报告写成 JSON")
    args = parser.parse_args(argv)

    report = asyncio.run(run(args))

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, default=str)

    if report["mismatched"]:
        print(f"✗ 维度不一致：{report['mismatched']} 行", file=sys.stderr)
        return 1
    print(
        f"✓ 维度一致：比对 {report['compared']} 行，"
        f"不一致 {report['mismatched']} 行，孤儿行 {len(report['orphanRows'])} 行"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
