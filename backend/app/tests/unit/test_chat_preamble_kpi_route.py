"""L1 preamble 的 KPI 路由：v1/v2 切换 + 歧义澄清短路（0 LLM）。

用真实 ``KpiSemanticMatchService`` + 内存缓存桩，只替换 mixin 的其余依赖
（``_buildL1Response`` / ``_handleNl2SqlAgent`` / 两个 DB 钩子），验证 preamble
的分支选择：v2 歧义 ⇒ L1C 澄清；v2 唯一命中 ⇒ L1；灰度关 ⇒ v1 行为不变。
"""

from __future__ import annotations

import pytest

from app.domain.schemas import ChatRequest
from app.services.chat_constants import ROUTING_LAYER_KPI_CLARIFY
from app.services.chat_stream_output import L1L4PreambleMixin
from app.services.kpi_semantic_match_service import KpiSemanticMatchService


class _Kpi:
    def __init__(self, code: str, name: str, keywords: list[str]) -> None:
        self.kpi_code = code
        self.kpi_name = name
        self.semantic_keywords = keywords


class _Cache:
    def __init__(self, kpis: list[_Kpi]) -> None:
        self._kpis = kpis

    def is_warmUp(self) -> bool:
        return True

    def getAll(self) -> list[_Kpi]:
        return list(self._kpis)

    def hasCode(self, code: str) -> bool:
        return any(k.kpi_code == code for k in self._kpis)

    def metricAliasOf(self, code: str) -> str | None:
        return None

    def findByAnyKeyword(self, keywords: list[str]) -> list[_Kpi]:
        return []


class _L1Response:
    """_buildL1Response 的返回值桩（只需 mixin 会读的字段）。"""

    def __init__(self, match) -> None:  # noqa: ANN001
        self.answer = f"L1:{match.code}"
        self.chartType = None
        self.chartOption = None
        self.visual_rationale = None
        self.kpi_name = "n"


class _Preamble(L1L4PreambleMixin):
    """只覆写外部依赖，路由逻辑（_routeKpi / _buildKpiClarifyPreamble）用真实现。"""

    def __init__(self, matcher: KpiSemanticMatchService, *, containment: bool) -> None:
        self._kpiMatcher = matcher
        self._containment = containment
        self.l4Called = False

    async def _containmentEnabled(self, session) -> bool:  # noqa: ANN001
        return self._containment

    async def _ensureKpiCacheFresh(self, session) -> None:  # noqa: ANN001
        """v2 前的缓存预热在纯单元测试里是 no-op（否则会打真实 DB）。"""

    async def _buildL1Response(self, match, session):  # noqa: ANN001
        return _L1Response(match)

    async def _resolveKpiCatalog(self, code, session):  # noqa: ANN001
        return None

    async def _handleNl2SqlAgent(self, session, dto, user):  # noqa: ANN001
        self.l4Called = True
        return None


_KPIS = [
    _Kpi("KPI_SUPPLIER_OTD", "供应商准时交付率", ["准时交付", "交付率"]),
    _Kpi("KPI_SUPPLIER_OVERDUE_RATIO", "供应商逾期率", ["逾期率"]),
]


@pytest.fixture()
def matcher() -> KpiSemanticMatchService:
    return KpiSemanticMatchService(_Cache(_KPIS))


def _dto(question: str) -> ChatRequest:
    return ChatRequest(question=question, sessionId="p1-session", datasourceId=1)


@pytest.mark.asyncio()
async def test_ambiguous_question_returns_clarify_without_l4(
    matcher: KpiSemanticMatchService,
) -> None:
    mixin = _Preamble(matcher, containment=True)
    preamble = await mixin._runL1AndL4Preamble(
        _dto("供应商交付率和逾期率分别是多少"), None, None
    )
    assert preamble is not None
    assert preamble.routing_layer == ROUTING_LAYER_KPI_CLARIFY
    assert "请确认" in preamble.answer
    assert mixin.l4Called is False  # 必须短路，不能落到 Agent Loop


@pytest.mark.asyncio()
async def test_clarify_response_has_zero_tokens_and_candidate_data(
    matcher: KpiSemanticMatchService,
) -> None:
    mixin = _Preamble(matcher, containment=True)
    preamble = await mixin._runL1AndL4Preamble(
        _dto("供应商交付率和逾期率分别是多少"), None, None
    )
    assert preamble is not None
    response = preamble.full_response
    assert response.intent == "clarify"
    assert response.tokensUsed == 0
    assert response.cost == 0.0
    assert [item["code"] for item in response.data] == [
        "KPI_SUPPLIER_OTD",
        "KPI_SUPPLIER_OVERDUE_RATIO",
    ]


@pytest.mark.asyncio()
async def test_unique_containment_hit_goes_to_l1(
    matcher: KpiSemanticMatchService,
) -> None:
    mixin = _Preamble(matcher, containment=True)
    preamble = await mixin._runL1AndL4Preamble(
        _dto("供应商准时交付率多少"), None, None
    )
    assert preamble is not None
    assert preamble.routing_layer == "L1"


@pytest.mark.asyncio()
async def test_containment_off_uses_legacy_match(
    matcher: KpiSemanticMatchService,
) -> None:
    """关灰度 ⇒ 走 v1：歧义问题不澄清（生产当前行为，必须可回退）。"""
    mixin = _Preamble(matcher, containment=False)
    preamble = await mixin._runL1AndL4Preamble(
        _dto("供应商交付率和逾期率分别是多少"), None, None
    )
    assert preamble is None or preamble.routing_layer != ROUTING_LAYER_KPI_CLARIFY
