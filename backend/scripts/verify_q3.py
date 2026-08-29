"""问题 3 完整端到端：plan 校验 → 生成 SQL → Oracle 真实执行。

用法：cd backend && uv run python scripts/verify_q3.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _loadSecretKey() -> None:
    env = Path(__file__).resolve().parents[2] / "docker" / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("SECRET_KEY="):
            os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()


_loadSecretKey()

from sqlalchemy import select  # noqa: E402

from app.domain.models import DataSource, LlmConfig  # noqa: E402
from app.infrastructure.business_db_pool import get_adapter  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.infrastructure.llm.factory import createClient  # noqa: E402
from app.services.nl2sql_service import Nl2SqlService  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402

QUESTION = sys.argv[1] if len(sys.argv) > 1 else "采购订单的到货情况"
DATASOURCE_ID = 2


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        ontology = OntologyService()
        classes = await ontology.listClasses(session)
        joins = await ontology.listJoins(session)
        cfg = (
            await session.execute(
                select(LlmConfig).where(LlmConfig.model_name == "deepseek-chat")
            )
        ).scalar_one()
        client = createClient(cfg)
        ds = await session.get(DataSource, DATASOURCE_ID)
        nl2sql = Nl2SqlService()

        planRes = await nl2sql.generateQueryPlan(
            QUESTION, classes, client, cfg,
            datasourceType=ds.type, oracle_version=ds.oracle_version,
            schemaPrefix=ds.username, context=None, priorState=None,
            fewShot=None, valueSamples=None, driftWarning=None,
            dictionaryText=None, joins=joins,
        )
        issues = nl2sql.validatePlan(planRes.plan, classes)
        if issues:
            print("plan 校验失败:", issues)
            return
        print("plan 校验通过 ✓")
        print("plan:", {
            "classes": planRes.plan.selectedClasses,
            "props": planRes.plan.selectedProperties,
            "aggs": [a.function + "(" + a.property + ")" + (f" AS {a.alias}" if a.alias else "") for a in planRes.plan.aggregations],
            "groupBy": list(planRes.plan.groupBy),
            "sortBy": [f"{s.property} {s.direction}" for s in planRes.plan.sortBy],
            "conditions": list(planRes.plan.conditions),
            "joins": [f"{j.sourceClass}-{j.targetClass}{list(j.columns)}" for j in planRes.plan.joins],
        })

        sqlRes = await nl2sql.generateSql(
            QUESTION, classes, client, cfg, plan=planRes.plan,
            datasourceType=ds.type, oracle_version=ds.oracle_version,
            schemaPrefix=ds.username, context=None, priorState=None,
            fewShot=None, valueSamples=None, driftWarning=None, joins=joins,
        )
        sql = (sqlRes.sql or "(空 SQL)").strip()
        print("=== 生成 SQL ===")
        print(sql)

        adapter = get_adapter(ds.id, ds)
        try:
            rows = await adapter.execute_read_only(sql)
        except Exception as exc:  # noqa: BLE001
            print(f"\n执行失败: {type(exc).__name__}: {exc}")
            return
        print(f"\nOracle 返回行数: {len(rows)}")
        for i, row in enumerate(rows[:3]):
            print(f"  [{i}] {row}")
        if rows:
            print("列:", list(rows[0].keys()))


if __name__ == "__main__":
    asyncio.run(main())
