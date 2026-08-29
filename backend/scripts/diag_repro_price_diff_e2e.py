"""真实链路端到端验证：用户报障问题走完整查询路径，确认修复后生成 SQL。

与 diag_repro_price_diff_real.py（镜像两阶段循环）不同，这里直接调用
_planAndGenerateSql：含主模型→备选模型降级、token 计量，是用户实际触达的路径。
主模型固定为 deepseek-chat；备选仍走 router（最便宜启用模型）。

用法：cd backend && uv run python scripts/diag_repro_price_diff_e2e.py
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

from app.domain.enums import IntentType  # noqa: E402
from app.domain.models import LlmConfig  # noqa: E402
from app.domain.schemas import ChatRequest  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.chat_service import ChatService  # noqa: E402

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
            sessionId="diag-price-diff-e2e", question=QUESTION, datasourceId=DATASOURCE_ID,
            modelId=cfg.id,
        )
        pc = await service._buildPipelineContext(session, dto)
        print(f"相关类 {len(pc.classes)} 个 | 主模型: {pc.selected.model_name} | "
              f"备选: {[c.model_name for c in pc.configs]}")
        outcome = await service._planAndGenerateSql(
            session, dto, pc, IntentType.NEW_QUERY, None,
        )
        if outcome.sql:
            print("=== 修复验证通过：真实链路生成 SQL ===")
            print(outcome.sql)
        else:
            print("=== 计划判定无法回答（非本次回归） ===")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
