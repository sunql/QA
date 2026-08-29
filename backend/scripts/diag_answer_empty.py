"""确证空结果 → answer 阶段 LLM 输出什么。

模拟 chat_service._generateAnswer 的空数据路径：query 结果 data=[] 时，
answer LLM 基于「查询结果：[]」生成回答，看是否产出『没有匹配到2025年…』。

用法：cd backend && uv run python scripts/diag_answer_empty.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from sqlalchemy import select  # noqa: E402

from app.domain.models import LlmConfig  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.infrastructure.llm.factory import createClient  # noqa: E402
from app.services.chat_service import ChatService  # noqa: E402
from app.services.chat_stream_output import _ANSWER_SYSTEM_PROMPT  # noqa: E402
from app.infrastructure.llm.base_client import LlmMessage  # noqa: E402

QUESTION = "统计查询2025年采购量最多的10种物料，2025年采购价格和2026年采购价格的差异"
SQL = """SELECT * FROM (
  SELECT t.ITMREF_0 AS 物料编号, t.TOTAL_QTY_2025, t.AVG_PRICE_2025
  FROM (SELECT ...) t WHERE 1=0
) WHERE ROWNUM <= 10"""


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        cfg = (
            await session.execute(
                select(LlmConfig).where(LlmConfig.model_name == "deepseek-chat")
            )
        ).scalar_one()
    client = createClient(cfg)
    prompt = ChatService._buildAnswerPrompt(QUESTION, SQL, [])
    resp = await client.complete(
        messages=[
            LlmMessage(role="system", content=_ANSWER_SYSTEM_PROMPT),
            LlmMessage(role="user", content=prompt),
        ],
        model=cfg.model_name,
    )
    print("=== answer LLM 基于空结果的回答 ===")
    print(resp.content)
    print("=== 传给 LLM 的 prompt ===")
    print(prompt[:600])


if __name__ == "__main__":
    asyncio.run(main())
