"""诊断问题 3 LLM plan：看选了哪些类、编造了哪些属性名、是否判定无法回答。

用法：cd backend && uv run python scripts/diagnose_q3.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path


def _loadSecretKey() -> None:
    env = Path(__file__).resolve().parents[2] / "docker" / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("SECRET_KEY="):
            os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()
            return


_loadSecretKey()

from sqlalchemy import select  # noqa: E402

from app.domain.models import DataSource, LlmConfig  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.infrastructure.llm.factory import createClient  # noqa: E402
from app.services.nl2sql_service import Nl2SqlService  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402

QUESTION = "采购订单的到货情况"
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
        res = await nl2sql.generateQueryPlan(
            QUESTION, classes, client, cfg,
            datasourceType=ds.type, oracle_version=ds.oracle_version,
            schemaPrefix=ds.username, context=None, priorState=None,
            fewShot=None, valueSamples=None, driftWarning=None,
            dictionaryText=None, joins=joins,
        )
        plan = res.plan
        print("=== 问题:", QUESTION, "===")
        print("target:", plan.target)
        print("isUnanswerable:", plan.isUnanswerable)
        print("selectedClasses:", plan.selectedClasses)
        print("selectedProperties:", plan.selectedProperties)
        print("aggregations:", plan.aggregations)
        print("joins:", plan.joins)
        print("conditions:", plan.conditions)
        print("groupBy:", plan.groupBy)
        print("interpretation:", plan.interpretation)
        print("--- 校验 ---")
        issues = nl2sql.validatePlan(plan, classes)
        print("issues:", issues)


if __name__ == "__main__":
    asyncio.run(main())
