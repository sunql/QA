"""循环复现问题 1（跨年价格差异）：统计 LLM 多次生成 SQL 的成功/失败分布。

目的：确认"同一问题多次生成 → 结果非确定"，并捕捉用户遇到的失败模式
（ORA 语法错 / 成功但 0 行 / 成功有数据）。每次迭代独立调 plan + generateSql。

用法：cd backend && uv run python scripts/diag_q1_loop.py [次数]
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from sqlalchemy import select  # noqa: E402

from app.domain.models import DataSource, LlmConfig  # noqa: E402
from app.infrastructure.business_db_pool import get_adapter  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.infrastructure.llm.factory import createClient  # noqa: E402
from app.services.nl2sql_service import Nl2SqlService  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402

QUESTION = "统计查询2025年采购量最多的10种物料，2025年采购价格和2026年采购价格的差异"
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
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
        adapter = get_adapter(ds.id, ds)
        nl2sql = Nl2SqlService()

        common = dict(
            classes=classes, llmClient=client, modelConfig=cfg,
            datasourceType=ds.type, oracle_version=ds.oracle_version,
            schemaPrefix=ds.username, context=None, priorState=None,
            fewShot=None, valueSamples=None, driftWarning=None,
            joins=joins,
        )
        planCommon = dict(common, dictionaryText=None)

        stats = {"ok": 0, "zero": 0, "ora": 0, "plan_fail": 0}
        for i in range(RUNS):
            planRes = await nl2sql.generateQueryPlan(QUESTION, **planCommon)
            issues = nl2sql.validatePlan(planRes.plan, classes)
            if issues:
                print(f"[{i}] plan 校验失败: {issues}")
                stats["plan_fail"] += 1
                continue
            plan = planRes.plan
            sqlRes = await nl2sql.generateSql(QUESTION, plan=plan, **common)
            sql = (sqlRes.sql or "").strip()
            try:
                rows = await adapter.execute_read_only(sql)
            except Exception as exc:  # noqa: BLE001
                msg = getattr(exc, "message", None) or str(exc)
                print(f"[{i}] ORA/执行失败: {msg[:120]}")
                print(f"    SQL 片段: {sql[:180]}")
                stats["ora"] += 1
                continue
            if not rows:
                print(f"[{i}] 成功但 0 行 ← 用户症状路径")
                print(f"    SQL: {sql[:200]}")
                stats["zero"] += 1
                continue
            print(f"[{i}] 成功 {len(rows)} 行 | 首行列: {list(rows[0].keys())}")
            stats["ok"] += 1

        print("\n=== 分布 ===", stats)


if __name__ == "__main__":
    asyncio.run(main())
