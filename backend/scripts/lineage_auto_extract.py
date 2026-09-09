"""Phase 2.2 lineage 自动提取种子脚本。

从 ontology (OntologyClass / OntologyJoin / OntologyMetric) 自动抽取血缘边
并写入 data_lineage 表。幂等：
- 同上下游 + 字段组合已存在 → 跳过（lineage_extractor 内置去重）
- 重复运行安全；新增 ontology 后再跑一次即可

运行：
    cd backend
    DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \\
      uv run python scripts/lineage_auto_extract.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.domain.models import DataLineage  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.lineage_extractor import extractEdges, persistEdges  # noqa: E402


async def _countExisting(session) -> int:
    """data_lineage 表现有行数（用于输出摘要）。"""
    stmt = select(DataLineage)
    result = await session.execute(stmt)
    return len(list(result.scalars().all()))


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        before = await _countExisting(session)
        print(f"[1/2] data_lineage 当前行数: {before}")

        edges = await extractEdges(session)
        print(f"[2/2] 抽取到 {len(edges)} 条新血缘边 ...")

        if edges:
            added = await persistEdges(session, edges)  # 与 POST /extract 共用落库逻辑
            print(f"  新增写入: {added}")
            after = await _countExisting(session)
            print(f"  data_lineage 现总行数: {after}")

            # 摘要：按 target_object 分布
            by_target: dict[str, int] = {}
            for edge in edges:
                key = f"{edge.target_layer.value}/{edge.target_object}"
                by_target[key] = by_target.get(key, 0) + 1
            print("\n按目标对象分布：")
            for key in sorted(by_target):
                print(f"  {key}: {by_target[key]} 条")
        else:
            print("  无新边可写（可能 ontology 为空或全部已存在）。")

    print("\n✅ Phase 2.2 lineage 自动提取完成。")
    print("   可访问 /api/v1/lineage/edges 验证。")
    print("   Phase 2.3 将基于此构建 ECharts 图可视化。")


if __name__ == "__main__":
    asyncio.run(main())