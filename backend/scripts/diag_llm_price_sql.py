"""端到端链路诊断：直接跑 LLM 生成 SQL，看实际 JOIN 和过滤逻辑"""
from __future__ import annotations

import asyncio
import os
import json
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from app.domain.models import LlmConfig
from app.domain.schemas import ChatRequest
from app.domain.enums import IntentType
from app.infrastructure.database import getSessionFactory
from app.services.chat_service import ChatService


QUESTION = (
    "统计查询2026年采购量最多的100种物料号为20、50开头的物料的供应商报价价格，"
    "把物料名称也显示出来，对比2025年该物料的供应商报价价格，将两个价格对比，按照价格差异排序"
)
DATASOURCE_ID = 2


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        cfg = (
            await session.execute(select(LlmConfig).where(LlmConfig.model_name == "deepseek-chat"))
        ).scalar_one()
        service = ChatService()
        dto = ChatRequest(
            sessionId="price-diag", question=QUESTION, datasourceId=DATASOURCE_ID,
            modelId=cfg.id,
        )
        pc = await service._buildPipelineContext(session, dto)
        print(f"候选类 {len(pc.classes)} 个: {[c.class_name for c in pc.classes]}")
        print(f"JOIN边 {len(pc.joins)} 条")
        print()
        outcome = await service._planAndGenerateSql(
            session, dto, pc, IntentType.NEW_QUERY, None,
        )
        print("=== 生成的 SQL ===")
        print(outcome.sql)
        print()
        if outcome.plan:
            print("=== 查询计划 ===")
            print(json.dumps(outcome.plan.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
