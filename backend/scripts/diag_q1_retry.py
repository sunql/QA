"""诊断问题 1（跨年价格差异）：完整复刻 chat_service._runQueryWithRetry 重试路径。

首版 SQL 执行 → 报错回灌 executionError → 重试 generateSql → 重试 SQL 再执行。
输出两次执行的 SQL 与最终行数，确认"用户看到没有匹配到2025年"是否源于空结果。

用法：cd backend && uv run python scripts/diag_q1_retry.py "<问题>"
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

QUESTION = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "统计查询2025年采购量最多的10种物料，2025年采购价格和2026年采购价格的差异"
)
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
        adapter = get_adapter(ds.id, ds)

        common = dict(
            classes=classes, llmClient=client, modelConfig=cfg,
            datasourceType=ds.type, oracle_version=ds.oracle_version,
            schemaPrefix=ds.username, context=None, priorState=None,
            fewShot=None, valueSamples=None, driftWarning=None,
            joins=joins,
        )
        # generateQueryPlan 接受 dictionaryText，generateSql 不接受
        planCommon = dict(common, dictionaryText=None)

        planRes = await nl2sql.generateQueryPlan(QUESTION, **planCommon)
        issues = nl2sql.validatePlan(planRes.plan, classes)
        if issues:
            print("plan 校验失败:", issues)
            return
        plan = planRes.plan

        def summarize(sql: str) -> None:
            print("  " + sql.replace("\n", "\n  ") if "\n" in sql else f"  {sql}")

        print("=== 首版生成 SQL ===")
        sqlRes = await nl2sql.generateSql(QUESTION, plan=plan, **common)
        sql = (sqlRes.sql or "(空 SQL)").strip()
        summarize(sql)

        print("\n=== 首版执行 ===")
        try:
            rows = await adapter.execute_read_only(sql)
            print(f"成功, {len(rows)} 行")
            for i, row in enumerate(rows[:3]):
                print(f"  [{i}] {row}")
            if rows:
                print("  列:", list(rows[0].keys()))
        except Exception as firstErr:
            msg = getattr(firstErr, "message", None) or str(firstErr)
            print(f"失败: {type(firstErr).__name__}: {msg}")
            print("\n=== 回灌错误重试 generateSql ===")
            retryRes = await nl2sql.generateSql(
                QUESTION, plan=plan, executionError=msg, maxRetries=0, **common
            )
            retrySql = (retryRes.sql or "(空 SQL)").strip()
            summarize(retrySql)
            print("\n=== 重试 SQL 执行 ===")
            try:
                rows = await adapter.execute_read_only(retrySql)
                print(f"成功, {len(rows)} 行")
                for i, row in enumerate(rows[:3]):
                    print(f"  [{i}] {row}")
            except Exception as secondErr:
                msg2 = getattr(secondErr, "message", None) or str(secondErr)
                print(f"仍失败: {type(secondErr).__name__}: {msg2}")
                print("→ 对外抛原始错误，用户会看到服务端错误而非空结果文案")
                return
        else:
            retrySql = sql

        if not rows:
            print("\n→ 最终 0 行，data=[] 传给 answer LLM → 用户看到『没有匹配到…』")
        else:
            print("\n→ 有数据，answer 阶段应正常汇报")


if __name__ == "__main__":
    asyncio.run(main())
