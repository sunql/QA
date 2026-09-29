"""复现「近五个月供应商供货量最大」计划校验失败（只读诊断，不改任何数据）。

打印：类召回窗口（含扩边诊断）→ 每次 plan 尝试的 initialErrors / 计划 JSON /
validatePlan 差异。真实 PG + 真实 Milvus + 真实 LLM（deepseek, modelId=1）。

用法（backend/ 目录下）：
    backend/.venv/bin/python backend/scripts/repro_supplier_top_supplier.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sys

from app.domain.enums import IntentType
from app.domain.schemas import ChatRequest
from app.infrastructure.database import getSessionFactory
from app.services.chat_service import ChatService
from app.services.nl2sql_service import Nl2SqlService

QUESTION = "截止今天往前推五个月的时间里面，那个供应商供货量最大，分别都供了什么物料，分别占比多少"

# 2026-09-16 05:08:37 生产失败现场快照：上一轮是「4月份有多少供应商下单」
# （PurchaseOrder COUNT），意图被判为 FOLLOW_UP，statePrompt 注入该轮状态。
PRIOR_QUESTION = "4月份有多少供应商下单"
PRIOR_SQL = (
    "SELECT COUNT(DISTINCT BPSNUM_0) AS SUPPLIER_COUNT\n"
    "FROM THBI.ODS_PORDER\n"
    "WHERE ORDDAT_0 >= TO_DATE('2026-04-01', 'YYYY-MM-DD')\n"
    "  AND ORDDAT_0 < TO_DATE('2026-05-01', 'YYYY-MM-DD')"
)
PRIOR_PLAN = {
    "target": "统计4月份下单的供应商数量",
    "selectedClasses": ["PurchaseOrder"],
    "selectedProperties": ["供应商"],
    "conditions": ["下单时间按订单日期ORDDAT_0过滤"],
    "aggregations": [
        {"function": "COUNT_DISTINCT", "property": "供应商", "alias": "supplier_count",
         "formula": None},
    ],
    "groupBy": [],
    "joins": [],
    "sortBy": [],
    "rowLimit": None,
    "partitionBy": [],
    "perGroupLimit": None,
    "interpretation": "供应商字段为BPSNUM_0，下单时间按订单日期ORDDAT_0过滤。",
}
RECENT_ROUNDS = [
    {"q": "在2026年实际到货的物料中，每种物料在所有到货物料中订的数量占比。", "s": "WITH ..."},
    {"q": "截止今天往前推五个月的时间里面，那个供应商供货量最大", "s": "WITH ..."},
]

_origGenerateQueryPlan = Nl2SqlService.generateQueryPlan


async def _tracedGenerateQueryPlan(self, question, classes, llmClient, modelConfig, **kw):
    result = await _origGenerateQueryPlan(self, question, classes, llmClient, modelConfig, **kw)
    print("\n=== plan attempt ===", flush=True)
    print("initialErrors:", kw.get("initialErrors"), flush=True)
    print(
        "plan:",
        json.dumps(dataclasses.asdict(result.plan), ensure_ascii=False, default=str),
        flush=True,
    )
    print("validatePlan:", self.validatePlan(result.plan, classes), flush=True)
    return result


Nl2SqlService.generateQueryPlan = _tracedGenerateQueryPlan


async def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    followUp = "--followup" in sys.argv
    chat = ChatService()
    dto = ChatRequest(
        sessionId="repro-supplier-top1", question=QUESTION,
        datasourceId=1, modelId=1,
    )
    async with getSessionFactory()() as session:
        pc = await chat._buildPipelineContext(session, dto)
        print("recall:", pc.recall, flush=True)
        print("schema classes:", sorted(c.class_name for c in pc.classes), flush=True)
        print("Supplier/PurchaseOrder in window:",
              any(c.class_name == "Supplier" for c in pc.classes),
              any(c.class_name == "PurchaseOrder" for c in pc.classes), flush=True)
        state = None
        intent = IntentType.NEW_QUERY
        if followUp:
            from app.domain.models import SessionQueryState
            state = SessionQueryState(
                session_id=dto.sessionId,
                last_question=PRIOR_QUESTION,
                last_plan=PRIOR_PLAN,
                last_sql=PRIOR_SQL,
                last_result_columns=["supplier_count"],
                recent_rounds=RECENT_ROUNDS,
            )
            intent = chat._intent.classifyResult(dto.question, hasPriorState=True).intent
            print("intent:", intent, flush=True)
        try:
            outcome = await chat._planAndGenerateSql(
                session, dto, pc, intent, state,
            )
            print("\nFINAL SQL:", outcome.sql, flush=True)
            return 0
        except Exception as exc:  # noqa: BLE001 — 诊断脚本，打印全部失败现场
            print(f"\nFAILED: {type(exc).__name__}: {exc}", flush=True)
            return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
