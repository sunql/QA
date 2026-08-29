"""A/B 对比：join description 注入对 NL2SQL 生成 SQL 的影响。

真实 LLM(deepseek-chat, temperature 已固定 0) + 真实本体(19 类 / 39 join)，
A = 注入 description（当前代码），B = 不注入（不可变副本 description=None）。
只跑到 SQL 生成阶段，不执行、不调 answer/chart，聚焦 description 对 JOIN 条件的影响。

用法（SECRET_KEY 用于解密 llm_config.api_key，同 smoke 脚本）：
    cd backend && \\
    SECRET_KEY=$(grep '^SECRET_KEY=' ../docker/.env | cut -d= -f2) \\
        uv run python scripts/ab_join_description.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path


def _loadSecretKey() -> None:
    """从 docker/.env 读完整 SECRET_KEY（base64 含 =，shell cut -d= -f2 会截断 padding）。"""
    envPath = Path(__file__).resolve().parents[2] / "docker" / ".env"
    if not envPath.exists():
        return
    for line in envPath.read_text().splitlines():
        if line.strip().startswith("SECRET_KEY="):
            os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()
            return


_loadSecretKey()

from sqlalchemy import select

from app.domain.models import DataSource, LlmConfig, OntologyJoin
from app.infrastructure.database import getSessionFactory
from app.infrastructure.llm.factory import createClient
from app.services.nl2sql_service import Nl2SqlService
from app.services.ontology_service import OntologyService

DATASOURCE_ID = 2  # ZJTH-Oracle（提供 schema 前缀 ZJTH. 与 Oracle 方言）

# ★ 复合键/跨单据流转（description 高价值）｜ ○ 简单外键（对照）
QUESTIONS = [
    "各供应商的收货明细条数",          # ○ 收货明细→收货单→供应商
    "到货明细对应的收货数量",          # ★ 到货明细↔收货明细(PTHNUM_0+PTDLIN_0)
    "采购订单的到货情况",              # ★ 订单明细↔到货明细(POHNUM_0+POPLIN_0)
    "各供应商的到货单数量",            # ○ 到货单→供应商(BPSNUM_0)
    "收货明细对应的采购订单行金额",    # ★ 收货明细↔订单明细(POHNUM_0+POPLIN_0)
]


def _stripDesc(joins: list[OntologyJoin]) -> list[OntologyJoin]:
    """构造 description=None 的不可变副本（不 mutate 原对象、不写库）。"""
    return [
        OntologyJoin(
            source_class_id=j.source_class_id,
            source_columns=list(j.source_columns),
            target_class_id=j.target_class_id,
            target_columns=list(j.target_columns),
            join_type=j.join_type,
            relation_type=j.relation_type,
            description=None,
            join_key=j.join_key,
        )
        for j in joins
    ]


async def _gen(question: str, strip: bool, factory) -> str:
    async with factory() as session:
        ontology = OntologyService()
        classes = await ontology.listClasses(session)
        joins = await ontology.listJoins(session)
        if strip:
            joins = _stripDesc(joins)
        cfg = (
            await session.execute(
                select(LlmConfig).where(LlmConfig.model_name == "deepseek-chat")
            )
        ).scalar_one()
        client = createClient(cfg)
        ds = await session.get(DataSource, DATASOURCE_ID)
        nl2sql = Nl2SqlService()
        try:
            planRes = await nl2sql.generateQueryPlan(
                question, classes, client, cfg,
                datasourceType=ds.type, oracle_version=ds.oracle_version, schemaPrefix=ds.username,
                context=None, priorState=None, fewShot=None,
                valueSamples=None, driftWarning=None, dictionaryText=None, joins=joins,
            )
        except Exception as e:
            return f"(生成plan异常: {type(e).__name__}: {str(e)[:80]})"
        issues = nl2sql.validatePlan(planRes.plan, classes)
        if issues:
            return "(plan校验失败: " + "; ".join(issues)[:140] + ")"
        if planRes.plan.isUnanswerable:
            return "(模型判定无法回答)"
        sqlRes = await nl2sql.generateSql(
            question, classes, client, cfg, plan=planRes.plan,
            datasourceType=ds.type, oracle_version=ds.oracle_version, schemaPrefix=ds.username,
            context=None, priorState=None, fewShot=None,
            valueSamples=None, driftWarning=None, joins=joins,
        )
        return (sqlRes.sql or "(空 SQL)").strip()


async def main() -> None:
    print("A/B 对比：join description 注入(A) vs 不注入(B)")
    print("LLM=deepseek-chat, temperature=0, 本体=19类/39join(6条带description)\n")
    factory = getSessionFactory()
    a = {q: await _gen(q, False, factory) for q in QUESTIONS}
    b = {q: await _gen(q, True, factory) for q in QUESTIONS}
    for q in QUESTIONS:
        same = a[q] == b[q]
        print(f"### {q}")
        print(f"  [A 注入]   {a[q]}")
        print(f"  [B 不注入] {b[q]}")
        print(f"  -> {'一致' if same else '不一致'}\n")


if __name__ == "__main__":
    asyncio.run(main())
