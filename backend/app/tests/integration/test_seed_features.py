"""Phase 4.3 seed_features 种子脚本集成测试（真实 PG 5433）。

覆盖：5 条核心特征幂等灌入（重复运行不新增）。
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.domain.models import FeatureDefinition
from scripts.seed_features import FEATURE_SEEDS, seedFeatures


async def _createTestDatasource(client) -> int:
    resp = await client.post(
        "/api/v1/datasources",
        json={
            "name": "seed-feature-ds",
            "type": "postgresql",
            "host": "db.example.com",
            "port": 5432,
            "databaseName": "appdb",
            "username": "u",
            "password": "p",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestSeedFeatures:
    async def test_seed_five_features_idempotent(self, client, dbSession) -> None:
        ds_id = await _createTestDatasource(client)

        first = await seedFeatures(dbSession, ds_id)
        assert first == 6  # 6 条特征全部 upsert（5 原有 + 1 PO_COMPLETION_RATE）

        # 6 条特征定义，名称与种子一致
        total = (
            await dbSession.execute(
                select(func.count()).select_from(FeatureDefinition)
            )
        ).scalar()
        assert total == 6
        names = {
            f["feature_name"] for f in FEATURE_SEEDS
        }
        assert names == {
            "SUPPLIER_OTD_3M",
            "SUPPLIER_DEFECT_RATE_3M",
            "SUPPLIER_PRICE_VARIANCE_3M",
            "SUPPLIER_RISK_SCORE",
            "MATERIAL_SHORTAGE_RISK",
            "SUPPLIER_PO_COMPLETION_RATE",
        }

        # 幂等：重复运行不产生重复数据（on_conflict_do_update，每次返回 affected rows=6，
        # 但 DB 内实际仍是 6 条，无新增）
        second = await seedFeatures(dbSession, ds_id)
        assert second == 6  # on_conflict_do_update 每行均报告 rowcount=1
        total_after = (
            await dbSession.execute(
                select(func.count()).select_from(FeatureDefinition)
            )
        ).scalar()
        assert total_after == 6
