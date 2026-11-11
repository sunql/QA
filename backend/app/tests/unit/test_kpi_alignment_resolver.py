"""KPI 别名解析层纯函数单测（P3 D2，零 IO）。

覆盖：恒等命中 / property_name 映射 / property_alias 映射 / business_aliases 映射 /
多候选 unknown / 无候选 missing / 纯时间词剔除 + TimeGrain 映射 / 归一化 /
空输入 / 无源类 / deriveTimeGrain 的 12 条真实 grain 字面量。

grain 字面量来自 recon §6 + `backend/scripts/seed_kpi_catalog.py`（11 条含「月」，
1 条 `供应商（快照）` 无时间词）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.domain.enums import TimeGrain
from app.services.kpi_alignment_resolver import (
    ResolutionOutcome,
    SourceProperty,
    deriveTimeGrain,
    resolveGovernanceTokens,
)


def _prop(
    property_name: str,
    source_column: str | None,
    *,
    property_alias: str | None = None,
    business_aliases: tuple[str, ...] = (),
    data_type: str | None = "VARCHAR",
) -> SourceProperty:
    return SourceProperty(
        property_name=property_name,
        source_column=source_column,
        property_alias=property_alias,
        business_aliases=business_aliases,
        data_type=data_type,
    )


# ── 1. 恒等命中 + 归一化 ──────────────────────────────────────────────


def testResolvesIdentitySourceColumn() -> None:
    outcome = resolveGovernanceTokens(("QTY",), (_prop("数量", "QTY"),))
    assert outcome.resolved == (("QTY", "QTY"),)
    assert outcome.unknownTokens == ()
    assert outcome.missingTokens == ()


def testCaseAndWhitespaceNormalization() -> None:
    props = (_prop("数量", "QTY"),)
    outcome = resolveGovernanceTokens((" qty ",), props)
    # 原始 token 保留原样；物理列取存储值原样
    assert outcome.resolved == ((" qty ", "QTY"),)


def testMultipleRowsSameSourceColumnCollapseToOne() -> None:
    props = (_prop("数量", "QTY"), _prop("数量别名", "QTY"))
    outcome = resolveGovernanceTokens(("QTY",), props)
    assert outcome.resolved == (("QTY", "QTY"),)
    assert outcome.unknownTokens == ()


# ── 2. property_name 映射 ──────────────────────────────────────────────


def testResolvesPropertyNameToSourceColumn() -> None:
    props = (_prop("收货数量", "RCV_QTY"),)
    outcome = resolveGovernanceTokens(("收货数量",), props)
    assert outcome.resolved == (("收货数量", "RCV_QTY"),)


def testPropertyNameWithoutSourceColumnIsUnknown() -> None:
    props = (_prop("收货数量", None),)
    outcome = resolveGovernanceTokens(("收货数量",), props)
    assert outcome.resolved == ()
    assert outcome.unknownTokens == ("收货数量",)
    assert outcome.missingTokens == ()


def testPropertyNameTakesPrecedenceOverAlias() -> None:
    props = (
        _prop("数量", "QTY_MAIN"),
        _prop("其他属性", "QTY_OTHER", property_alias="数量"),
    )
    outcome = resolveGovernanceTokens(("数量",), props)
    assert outcome.resolved == (("数量", "QTY_MAIN"),)
    assert outcome.unknownTokens == ()


def testIdentityTakesPrecedenceOverPropertyName() -> None:
    props = (
        _prop("数量", "QTY"),
        _prop("QTY", "QTY_OTHER"),
    )
    outcome = resolveGovernanceTokens(("QTY",), props)
    assert outcome.resolved == (("QTY", "QTY"),)


# ── 3. property_alias 映射 ─────────────────────────────────────────────


def testResolvesPropertyAlias() -> None:
    props = (_prop("数量", "QTY", property_alias="数量值"),)
    outcome = resolveGovernanceTokens((" 数量值 ",), props)
    assert outcome.resolved == ((" 数量值 ", "QTY"),)
    assert outcome.unknownTokens == ()


def testAliasWithoutSourceColumnIsUnknown() -> None:
    props = (_prop("数量", None, property_alias="数量值"),)
    outcome = resolveGovernanceTokens(("数量值",), props)
    assert outcome.unknownTokens == ("数量值",)


# ── 4. business_aliases 映射 ───────────────────────────────────────────


def testResolvesBusinessAlias() -> None:
    props = (_prop("数量", "QTY", business_aliases=("数量", "Quantity")),)
    outcome = resolveGovernanceTokens(("quantity",), props)
    assert outcome.resolved == (("quantity", "QTY"),)


def testBusinessAliasesMultiCandidateIsUnknown() -> None:
    props = (
        _prop("A", "COL_A", business_aliases=("金额",)),
        _prop("B", "COL_B", business_aliases=("金额",)),
    )
    outcome = resolveGovernanceTokens(("金额",), props)
    assert outcome.resolved == ()
    assert outcome.unknownTokens == ("金额",)


# ── 5. 多候选 unknown / 无候选 missing ─────────────────────────────────


def testMultipleCandidateColumnsAreUnknown() -> None:
    props = (
        _prop("数量", "QTY_A"),
        _prop("数量", "QTY_B"),
    )
    outcome = resolveGovernanceTokens(("数量",), props)
    assert outcome.resolved == ()
    assert outcome.unknownTokens == ("数量",)
    assert outcome.missingTokens == ()


def testUnmatchedTokenIsMissing() -> None:
    props = (_prop("数量", "QTY"),)
    outcome = resolveGovernanceTokens(("不存在的词",), props)
    assert outcome.resolved == ()
    assert outcome.unknownTokens == ()
    assert outcome.missingTokens == ("不存在的词",)


def testBlankTokenIsMissing() -> None:
    props = (_prop("数量", "QTY"),)
    outcome = resolveGovernanceTokens(("   ",), props)
    assert outcome.missingTokens == ("   ",)


# ── 6. 纯时间词（R8） ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("月", TimeGrain.MONTH),
        ("月份", TimeGrain.MONTH),
        ("按月", TimeGrain.MONTH),
        ("季度", TimeGrain.QUARTER),
        ("按季度", TimeGrain.QUARTER),
        ("年", TimeGrain.YEAR),
        ("按年", TimeGrain.YEAR),
        ("周", TimeGrain.WEEK),
        ("按周", TimeGrain.WEEK),
        ("日", TimeGrain.DAY),
        ("按日", TimeGrain.DAY),
        ("天", TimeGrain.DAY),
        ("按天", TimeGrain.DAY),
        (" 按月 ", TimeGrain.MONTH),
    ],
)
def testPureTimeTokenMapsToTimeGrain(token: str, expected: TimeGrain) -> None:
    props = (_prop("数量", "QTY"),)
    outcome = resolveGovernanceTokens((token,), props)
    assert outcome.timeGrain is expected
    assert outcome.resolved == ()
    assert outcome.unknownTokens == ()
    assert outcome.missingTokens == ()


def testTimeWordExcludedFromColumnResolutionEvenWhenColumnExists() -> None:
    props = (_prop("月", "月"),)
    outcome = resolveGovernanceTokens(("月",), props)
    assert outcome.timeGrain is TimeGrain.MONTH
    assert outcome.resolved == ()


def testConflictingTimeWordsAreUnknown() -> None:
    props = (_prop("数量", "QTY"),)
    outcome = resolveGovernanceTokens(("月", "季度"), props)
    assert outcome.timeGrain is None
    assert outcome.unknownTokens == ("月", "季度")
    assert outcome.resolved == ()
    assert outcome.missingTokens == ()


def testSameGrainTimeWordsDoNotConflict() -> None:
    outcome = resolveGovernanceTokens(("月", "月份"), (_prop("数量", "QTY"),))
    assert outcome.timeGrain is TimeGrain.MONTH
    assert outcome.unknownTokens == ()


# ── 7. timeCandidates ──────────────────────────────────────────────────


def testTimeCandidatesCollectDateTypedAndDateNamedColumns() -> None:
    props = (
        _prop("订单日期", "ORDER_DATE", data_type="DATE"),
        _prop("创建时间", "CREATED_AT", data_type="datetime"),
        _prop("更新时间", "UPDATED_TS", data_type="TIMESTAMP"),
        _prop("收货日期", "GOODS_RECEIPT_DATE", data_type="VARCHAR"),
        _prop("源时间", "SOURCE_TS", data_type="varchar"),
        _prop("数量", "QTY", data_type="VARCHAR"),
        _prop("无列", None, data_type="DATE"),
        _prop("订单日期重复", "ORDER_DATE", data_type="DATE"),
    )
    outcome = resolveGovernanceTokens((), props)
    assert outcome.timeCandidates == (
        "CREATED_AT",
        "GOODS_RECEIPT_DATE",
        "ORDER_DATE",
        "SOURCE_TS",
        "UPDATED_TS",
    )


# ── 8. 空输入 / 无源类 / 混合顺序 ───────────────────────────────────────


def testEmptyTokens() -> None:
    props = (_prop("数量", "QTY"), _prop("订单日期", "ORDER_DATE", data_type="DATE"))
    for tokens in (None, (), []):
        outcome = resolveGovernanceTokens(tokens, props)
        assert outcome.resolved == ()
        assert outcome.unknownTokens == ()
        assert outcome.missingTokens == ()
        assert outcome.timeGrain is None
        assert outcome.timeCandidates == ("ORDER_DATE",)


def testNoSourceClassesMakesEveryTokenMissing() -> None:
    outcome = resolveGovernanceTokens(("QTY", "数量"), ())
    assert outcome.resolved == ()
    assert outcome.unknownTokens == ()
    assert outcome.missingTokens == ("QTY", "数量")
    assert outcome.timeCandidates == ()


def testMixedOutcomePreservesInputOrder() -> None:
    props = (
        _prop("数量", "QTY"),
        _prop("收货数量", "RCV_QTY", property_alias="收货量"),
    )
    outcome = resolveGovernanceTokens(("数量", "月", "缺失", "收货量"), props)
    assert outcome.resolved == (("数量", "QTY"), ("收货量", "RCV_QTY"))
    assert outcome.missingTokens == ("缺失",)
    assert outcome.timeGrain is TimeGrain.MONTH


# ── 9. deriveTimeGrain：12 条真实 grain 字面量 ────────────────────────


_REAL_GRAIN_VALUES = [
    "供应商+工厂+月",
    "供应商+工厂+月",
    "供应商+工厂+月",
    "供应商+工厂+月",
    "供应商+物料+工厂+月",
    "供应商+物料+工厂+月",
    "供应商+物料+工厂+月",
    "物料+工厂+月",
    "供应商+物料+月",
    "供应商+月",
    "供应商+月",
    "供应商（快照）",
]


@pytest.mark.parametrize(
    ("grainText", "expected"),
    [(text, TimeGrain.MONTH) for text in _REAL_GRAIN_VALUES[:11]] + [(_REAL_GRAIN_VALUES[11], None)],
)
def testDeriveTimeGrainFromRealGrainValues(grainText: str, expected: TimeGrain | None) -> None:
    assert deriveTimeGrain(grainText) is expected


@pytest.mark.parametrize(
    ("grainText", "expected"),
    [
        ("季度累计", TimeGrain.QUARTER),
        ("按年汇总", TimeGrain.YEAR),
        ("周口径", TimeGrain.WEEK),
        ("日均", TimeGrain.DAY),
        ("以天为单位", TimeGrain.DAY),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def testDeriveTimeGrainOtherTexts(grainText: str | None, expected: TimeGrain | None) -> None:
    assert deriveTimeGrain(grainText) is expected


# ── 10. 不可变契约 ─────────────────────────────────────────────────────


def testSourcePropertyIsFrozenWithDefaults() -> None:
    prop = SourceProperty(property_name="数量", source_column="QTY", property_alias=None)
    assert prop.business_aliases == ()
    assert prop.data_type is None
    with pytest.raises(FrozenInstanceError):
        prop.source_column = "OTHER"  # type: ignore[misc]


def testResolutionOutcomeIsFrozenWithDefaults() -> None:
    outcome = ResolutionOutcome()
    assert outcome.resolved == ()
    assert outcome.unknownTokens == ()
    assert outcome.missingTokens == ()
    assert outcome.timeGrain is None
    assert outcome.timeCandidates == ()
    with pytest.raises(FrozenInstanceError):
        outcome.resolved = ()  # type: ignore[misc]
