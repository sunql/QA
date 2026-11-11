"""P1 匹配器 v2：归一化包含 + 最长短语胜出 + 等长歧义（设计 §5.1/§5.2）。

纯算法，无 DB：``_RouteCache`` 是内存桩，只实现 matchRoute 需要的四个方法。
每条用例都对应一个可以通过变异让它变红的判别点（plan D5 有变异取证清单）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest
import pytest_asyncio

from app.services.kpi_semantic_match_service import (
    KpiMatchResult,
    KpiMatchRoute,
    KpiSemanticMatchService,
    buildKpiClarifyAnswer,
    isEligiblePhrase,
    normalizeKpiText,
    phraseMatchesQuestion,
)

# ---------------------------------------------------------------------------
# Override autouse fixtures from conftest.py that pull in DB dependency.
# These tests are pure algorithm / in-memory stub — no DB needed.
# ---------------------------------------------------------------------------

@pytest.fixture()
def dbSession() -> Any:
    """Override conftest autouse dbSession — not used by these tests."""
    return None


@pytest_asyncio.fixture()
async def seedEngine() -> Any:
    """Override conftest autouse seedEngine — not used by these tests."""
    return None


@pytest_asyncio.fixture()
async def warmBusinessObjectRegistry() -> Any:
    """Override conftest autouse warmBusinessObjectRegistry — no-op, no DB needed."""
    yield


# ---------------------------------------------------------------------------
# 桩：只实现 matchRoute 用到的四个方法（duck typing，不继承 KpiMatchCache）
# ---------------------------------------------------------------------------

class _Kpi:
    def __init__(self, code: str, name: str, keywords: list[str] | None) -> None:
        self.kpi_code = code
        self.kpi_name = name
        self.semantic_keywords = keywords


class _RouteCache:
    def __init__(self, kpis: list[_Kpi], aliases: dict[str, str] | None = None) -> None:
        self._kpis = list(kpis)
        self._aliases = dict(aliases or {})
        self._warm = True

    def is_warmUp(self) -> bool:
        return self._warm

    def getAll(self) -> list[_Kpi]:
        return list(self._kpis)

    def hasCode(self, code: str) -> bool:
        return any(k.kpi_code == code for k in self._kpis)

    def metricAliasOf(self, code: str) -> str | None:
        return self._aliases.get(code)


_OTD = _Kpi(
    "KPI_SUPPLIER_OTD",
    "供应商准时交付率",
    ["准时交付", "OTD", "交付率", "on time delivery"],
)
_OVERDUE = _Kpi("KPI_SUPPLIER_OVERDUE_RATIO", "供应商逾期率", ["逾期率", "逾期订单"])
_DELAY = _Kpi("KPI_SUPPLIER_DELAY_DAYS", "供应商平均延误天数", ["逾期天数", "延误"])


@pytest.fixture()
def svc() -> KpiSemanticMatchService:
    return KpiSemanticMatchService(_RouteCache([_OTD, _OVERDUE, _DELAY]))


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

def test_normalize_fullwidth_and_punctuation_become_spaces() -> None:
    """全角标点／连字符必须变成分隔符：否则 "on-time" 会粘成 "ontime"。"""
    assert normalizeKpiText("Ｏｎ－Time　Delivery！") == "on time delivery"


def test_normalize_collapses_whitespace() -> None:
    assert normalizeKpiText("  供应商   交付率  ") == "供应商 交付率"


def test_normalize_empty_string() -> None:
    assert normalizeKpiText("") == ""


# ---------------------------------------------------------------------------
# 短语门槛
# ---------------------------------------------------------------------------

def test_single_cjk_char_phrase_is_ineligible() -> None:
    """单字中文短语一律不合格（"险" 会命中一半问题）。"""
    assert isEligiblePhrase("险") is False


def test_two_cjk_char_phrase_is_eligible() -> None:
    assert isEligiblePhrase("逾期") is True


def test_two_letter_ascii_phrase_is_ineligible() -> None:
    assert isEligiblePhrase("ok") is False


def test_three_letter_ascii_phrase_is_eligible() -> None:
    assert isEligiblePhrase("OTD") is True


def test_mixed_phrase_eligible_by_ascii_side() -> None:
    """1 个中文 + 3 个字母：ASCII 侧达标即可。"""
    assert isEligiblePhrase("NCR率") is True


# ---------------------------------------------------------------------------
# 包含判定
# ---------------------------------------------------------------------------

def test_cjk_phrase_matches_as_substring() -> None:
    assert phraseMatchesQuestion("逾期率", normalizeKpiText("本月逾期率是多少")) is True


def test_ascii_phrase_requires_token_boundary() -> None:
    """关键判别点：OTD 不能命中 notdone / otdx。"""
    assert phraseMatchesQuestion("OTD", normalizeKpiText("notdone")) is False
    assert phraseMatchesQuestion("OTD", normalizeKpiText("otdx 是多少")) is False
    assert phraseMatchesQuestion("OTD", normalizeKpiText("算一下 OTD 是多少")) is True


def test_ascii_phrase_matches_across_hyphen() -> None:
    """归一化把连字符变空格后，"on time delivery" 能命中 "on-time delivery"。"""
    assert phraseMatchesQuestion("on time delivery", normalizeKpiText("on-time delivery")) is True


def test_empty_phrase_never_matches() -> None:
    assert phraseMatchesQuestion("", normalizeKpiText("任何问题")) is False


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@pytest.mark.asyncio()
async def test_exact_code_wins_with_confidence_one(svc: KpiSemanticMatchService) -> None:
    route = await svc.matchRoute("KPI_SUPPLIER_OTD 最近怎么样")
    assert route.kind == "hit"
    assert len(route.matches) == 1
    assert route.matches[0].code == "KPI_SUPPLIER_OTD"
    assert route.matches[0].confidence == 1.0
    assert route.matches[0].method == "exact_code"


@pytest.mark.asyncio()
async def test_single_containment_hit(svc: KpiSemanticMatchService) -> None:
    route = await svc.matchRoute("哪些供应商的交付率低")
    assert route.kind == "hit"
    assert [m.code for m in route.matches] == ["KPI_SUPPLIER_OTD"]
    assert route.matches[0].method == "containment"
    assert route.matches[0].matched_phrase == "交付率"


@pytest.mark.asyncio()
async def test_longest_phrase_wins(svc: KpiSemanticMatchService) -> None:
    """「逾期天数」4 字属于 DELAY，OVERDUE 只有 2 字「逾期」⇒ DELAY 胜出。"""
    route = await svc.matchRoute("逾期天数排行")
    assert route.kind == "hit"
    assert [m.code for m in route.matches] == ["KPI_SUPPLIER_DELAY_DAYS"]
    assert route.matches[0].matched_phrase == "逾期天数"


@pytest.mark.asyncio()
async def test_equal_length_cross_kpi_is_ambiguous_sorted_by_code(
    svc: KpiSemanticMatchService,
) -> None:
    """3+3 并列且分属两个 KPI ⇒ ambiguous，候选按 code 升序（确定性）。"""
    route = await svc.matchRoute("供应商交付率和逾期率分别是多少")
    assert route.kind == "ambiguous"
    assert [m.code for m in route.matches] == [
        "KPI_SUPPLIER_OTD",
        "KPI_SUPPLIER_OVERDUE_RATIO",
    ]


@pytest.mark.asyncio()
async def test_no_match_returns_none(svc: KpiSemanticMatchService) -> None:
    route = await svc.matchRoute("供应商一共有多少家")
    assert route.kind == "none"
    assert route.matches == ()


@pytest.mark.asyncio()
async def test_empty_question_returns_none(svc: KpiSemanticMatchService) -> None:
    route = await svc.matchRoute("   ")
    assert route.kind == "none"


@pytest.mark.asyncio()
async def test_cold_cache_returns_none() -> None:
    cache = _RouteCache([_OTD])
    cache._warm = False
    route = await KpiSemanticMatchService(cache).matchRoute("供应商交付率")
    assert route.kind == "none"


@pytest.mark.asyncio()
async def test_metric_alias_is_a_phrase_source() -> None:
    """KPI 挂靠 metric 的别名参与匹配（设计 §5.1 第三个短语源）。"""
    cache = _RouteCache([_Kpi("KPI_X", "X 指标", [])], aliases={"KPI_X": "准时交货"})
    route = await KpiSemanticMatchService(cache).matchRoute("这个月准时交货情况")
    assert route.kind == "hit"
    assert route.matches[0].code == "KPI_X"
    assert route.matches[0].matched_phrase == "准时交货"


def test_route_is_frozen() -> None:
    route = KpiMatchRoute(kind="none")
    with pytest.raises(FrozenInstanceError):
        route.kind = "hit"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 澄清文案
# ---------------------------------------------------------------------------

def test_clarify_answer_lists_every_candidate() -> None:
    text = buildKpiClarifyAnswer(
        "供应商交付率和逾期率分别是多少",
        (
            KpiMatchResult(code="KPI_SUPPLIER_OTD", confidence=0.9, kpi_name="供应商准时交付率"),
            KpiMatchResult(code="KPI_SUPPLIER_OVERDUE_RATIO", confidence=0.9, kpi_name="供应商逾期率"),
        ),
    )
    assert "供应商准时交付率" in text
    assert "KPI_SUPPLIER_OVERDUE_RATIO" in text
    assert "供应商交付率和逾期率分别是多少" in text
    # 必须明确要求用户确认，不能直接给数字
    assert "确认" in text
