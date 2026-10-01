"""端到端重试测试：语句形态 formula 的引导真的改变了模型的下一步。

2026-10-01 线上回归的另一半。单元测试只证明「反馈现在是对的」；本测试证明
**这条反馈会被送进下一次尝试，且模型据此改写后能通过校验**——若只改反馈文案
而没接进重试链路，这里会红。

回归背景：用户问「5月份供货量最多的三家供应商所供货物总量占5月份总供货量的
比例是多少」，LLM 把整条 SELECT 放进 Aggregation.formula，schema 名/表名/表别名/
ONLY 被逐 token 误报成属性幻觉（用户侧 6 条），重试反馈无指向 → 模型原样重犯 →
maxPlanAttempts 耗尽 → 整轮失败。

运行：
    TEST_DATABASE_URL=postgresql+asyncpg://qa_user:pass@localhost:5434/qa_metadata_test \\
        uv run pytest app/tests/integration/test_nl2sql_structural_formula_retry.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.models import OntologyClass, OntologyProperty
from app.infrastructure.llm.base_client import LlmMessage, LlmResponse
from app.services.nl2sql_service import Nl2SqlService


class _FakeLlm:
    """按顺序弹出预置回复的假客户端，记录每次调用的消息。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(
        self,
        messages: list[LlmMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        maxTokens: int | None = None,
        **kwargs: Any,
    ) -> LlmResponse:
        self.calls.append([(m.role, m.content) for m in messages])
        content = self._responses.pop(0)
        return LlmResponse(
            content=content,
            modelName=model or "test-model",
            promptTokens=10,
            completionTokens=5,
            totalTokens=15,
        )

    def userPrompts(self) -> list[str]:
        return ["\n".join(c for role, c in call if role == "user") for call in self.calls]


def _cls() -> OntologyClass:
    return OntologyClass(
        class_name="PRECEIPT",
        source_table="T_PRECEIPT",
        properties=[
            OntologyProperty(property_name="BPSNUM", source_column="BPSNUM_0"),
            OntologyProperty(property_name="QTY", source_column="QTY_0"),
        ],
    )


# 第 1 次尝试：语句形态 formula（含 FROM/JOIN）。
# 修复前：表名/schema 名/表别名/ONLY 被逐条误报成属性幻觉。
_BAD_PLAN = (
    '{"target":"前三家供应商供货量占比","selectedClasses":["PRECEIPT"],'
    '"selectedProperties":["BPSNUM","QTY"],'
    '"aggregations":[{"function":"SUM","property":"QTY","alias":"占比","formula":'
    '"SELECT SUM(d2.RCV_QTY) FROM THBI.DWD_GOODS_RECEIPT_DTL d2 '
    'JOIN THBI.DIM_IMATERIAL m2 ON d2.ITEM_CODE = m2.ITMREF_0 '
    'FETCH FIRST 3 ROWS ONLY"}],'
    '"groupBy":[],"sortBy":[],"joins":[],"conditions":[]}'
)

# 第 2 次尝试：照着引导改用 CTE 形态。
_GOOD_PLAN = (
    '{"target":"前三家供应商供货量占比","selectedClasses":["PRECEIPT"],'
    '"selectedProperties":["BPSNUM","QTY"],'
    '"aggregations":[{"function":"SUM","property":"QTY","alias":"占比","formula":'
    '"WITH top3 AS (SELECT BPSNUM, SUM(QTY) AS qty FROM T_PRECEIPT '
    'GROUP BY BPSNUM ORDER BY qty DESC FETCH FIRST 3 ROWS ONLY) '
    'SELECT SUM(qty) AS 占比 FROM top3"}],'
    '"groupBy":[],"sortBy":[],"joins":[],"conditions":[]}'
)


@pytest.mark.integration
async def test_structural_formula_hint_reaches_retry_and_recovers(dbSession):
    """语句形态计划 → 校验失败并回注引导 → 第 2 次 CTE 计划通过。"""
    nl2sql = Nl2SqlService()
    llm = _FakeLlm(responses=[_BAD_PLAN, _GOOD_PLAN])
    model_config = SimpleNamespace(model_name="test-model", temperature=0.0)

    result = await nl2sql.generateValidatedPlan(
        "5月份供货量最多的三家供应商所供货物总量占5月份总供货量的比例是多少",
        [_cls()],
        llm,
        model_config,
        datasourceType="POSTGRESQL",
        session=dbSession,
    )

    # 1) 确实重试了一次（语句计划被拒 → 第二次尝试）
    assert len(llm.calls) == 2, f"期望恰好 1 次重试，实际 LLM 调用 {len(llm.calls)} 次"

    # 2) 重试 prompt 里带着那条可操作引导。
    #    断言到「CTE 形式」这一句 —— 它在提示语末尾，能同时证明
    #    _ERROR_SNIPPET_LIMIT=200 没有把引导截断（截断从尾部砍）。
    retryPrompt = llm.userPrompts()[1]
    assert "formula 不能是整条 SQL 语句" in retryPrompt, retryPrompt[-400:]
    assert "窗口函数" in retryPrompt, retryPrompt[-400:]
    assert "CTE 形式" in retryPrompt, retryPrompt[-400:]

    # 3) 最终拿到的是遵守引导后的 CTE 计划，而不是抛 Nl2SqlError
    agg = result.plan.aggregations[0]
    assert agg.formula.startswith("WITH "), agg.formula
