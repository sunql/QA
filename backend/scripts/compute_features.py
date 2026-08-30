"""Phase 4.3 AI 特征计算入口（一次性脚本 + 手动触发）。

计算所有 is_enabled=true 且 status=ACTIVE 的特征，把 calculation_logic 按只读护栏
执行到业务库（THBI），解析结果行并幂等 upsert 到 feature_value。

业务库连接经 business_db_pool.get_adapter 解析（数据源密码密文解密，无明文）。

运行：
    cd backend
    DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
      ./.venv/bin/python scripts/compute_features.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.schemas import FeatureComputeResult  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.feature_compute_service import FeatureComputeService  # noqa: E402


async def computeAll(session) -> list[FeatureComputeResult]:
    """计算全部 enabled + ACTIVE 特征，返回各特征落库行数。"""
    return await FeatureComputeService().computeAllEnabled(session)


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        results = await computeAll(session)
        total_rows = sum(r.rows for r in results)
    print(f"[compute_features] 计算 {len(results)} 个特征，共 {total_rows} 行特征值")
    for r in results:
        print(f"  - feature_id={r.feature_id}: {r.rows} 行")
    print("✅ AI 特征计算完成（幂等 upsert 到 feature_value）")


if __name__ == "__main__":
    asyncio.run(main())
