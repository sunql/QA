"""visual_rationale 纯函数模块：code/params 契约 + 降级 + 不可变。

设计要点（task-1-brief）：
- 21 个 code 全集 = chart_decision 的 18 个 ruleId + chart_service 的
  R_FORCED_CLIENT + 2 个新常量（DEGRADE_SPEC_INVALID / SUMMARY_TEXT_ONLY）。
- params 只放插值变量（rows/kind），不放文案（文案在前端 i18n）。
- 未知 ruleId 不抛异常，落 R14_DEFAULT_TABLE 兜底 + logger.warning。
- VisualRationale 必须 frozen。
"""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError
from enum import Enum

import pytest

from app.domain.enums import ChartType
from app.services.visual_rationale import (
    VisualRationale,
    buildVisualRationale,
    summaryTextOnlyRationale,
)

# chart_decision 的 18 个 ruleId。rowCount=5 固定，用来验证 params 形状。
_PASSTHROUGH_CASES: list[tuple[str, dict]] = [
    ("R00_EMPTY_TABLE", {}),
    ("R01_SINGLE_VALUE_KPI", {}),
    ("R01S_SINGLE_ROW_TABLE", {}),
    ("R02_SHARE_DONUT", {"rows": 5}),
    ("R03_SHARE_OVERFLOW_HBAR", {"rows": 5}),
    ("R04_TOPN_HBAR", {"rows": 5}),
    ("R05_WATERFALL", {}),
    ("R06_COMBO", {}),
    ("R07_TREND_LINE", {}),
    ("R08_RELATION_SCATTER", {}),
    ("R09_MULTIDIM_HEATMAP", {}),
    ("R10_MULTIDIM_BAR", {}),
    ("R11_HBAR_MANY_ROWS", {"rows": 5}),
    ("R12S_QUESTION_SHARE_DONUT", {}),
    ("R12S_QUESTION_SHARE_HBAR", {}),
    ("R12_CATEGORY_BAR", {}),
    ("R13_RAW_DETAIL_TABLE", {}),
    ("R14_DEFAULT_TABLE", {}),
]


@pytest.mark.parametrize("ruleId, expectedParams", _PASSTHROUGH_CASES)
def test_passthrough_code_and_params(ruleId: str, expectedParams: dict) -> None:
    r = buildVisualRationale(ruleId=ruleId, kind=ChartType.TABLE, rowCount=5)
    assert r.code == ruleId
    assert r.params == expectedParams


def test_forced_client_carries_kind() -> None:
    r = buildVisualRationale(ruleId="R_FORCED_CLIENT", kind=ChartType.BAR, rowCount=5)
    assert r.code == "R_FORCED_CLIENT"
    assert r.params == {"kind": ChartType.BAR}


def test_degrade_reason_overrides_rule_id() -> None:
    r = buildVisualRationale(
        ruleId="R07_TREND_LINE", kind=ChartType.LINE, rowCount=3,
        degradeReason="spec 校验失败",
    )
    assert r.code == "DEGRADE_SPEC_INVALID"
    assert r.params == {"kind": ChartType.LINE}


def test_degrade_reason_wins_over_unknown_rule_id() -> None:
    r = buildVisualRationale(
        ruleId="R_WEIRD_UNKNOWN", kind=ChartType.TABLE, rowCount=0, degradeReason="bad",
    )
    assert r.code == "DEGRADE_SPEC_INVALID"
    assert r.params == {"kind": ChartType.TABLE}


def test_unknown_rule_id_falls_back_to_default_table(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.services.visual_rationale"):
        r = buildVisualRationale(ruleId="R_WEIRD_UNKNOWN", kind=ChartType.TABLE, rowCount=1)
    assert r.code == "R14_DEFAULT_TABLE"
    assert r.params == {}
    assert any("R_WEIRD_UNKNOWN" in rec.getMessage() for rec in caplog.records)


def test_unknown_rule_id_does_not_raise() -> None:
    r = buildVisualRationale(ruleId="not-a-rule", kind=ChartType.TABLE, rowCount=0)
    assert r.code == "R14_DEFAULT_TABLE"


def test_summary_text_only() -> None:
    r = summaryTextOnlyRationale()
    assert r.code == "SUMMARY_TEXT_ONLY"
    assert r.params == {}


def test_visual_rationale_is_frozen() -> None:
    r = buildVisualRationale(ruleId="R02_SHARE_DONUT", kind=ChartType.DONUT, rowCount=3)
    with pytest.raises(FrozenInstanceError):
        r.code = "R14_DEFAULT_TABLE"


def test_rows_param_is_int() -> None:
    r = buildVisualRationale(ruleId="R11_HBAR_MANY_ROWS", kind=ChartType.HBAR, rowCount=42)
    assert r.params == {"rows": 42}


def test_visual_rationale_type() -> None:
    assert isinstance(summaryTextOnlyRationale(), VisualRationale)


def test_to_dict_normalizes_enum_kind_to_value() -> None:
    """线上形状（Task 4 契约贯穿）：params 里的 ChartType 枚举成员必须归一成 .value。

    `ChartType.BAR == "bar"` 为 True（`(str, Enum)` 的真值），单纯相等断言无法区分
    归一与未归一 —— 把 to_dict 的归一整个删掉，`== "bar"` 照样通过。故用
    `not isinstance(..., Enum)` 钉死「读端拿到的是标量、不是枚举实例」。
    """
    r = buildVisualRationale(ruleId="R_FORCED_CLIENT", kind=ChartType.BAR, rowCount=5)
    assert r.to_dict() == {"code": "R_FORCED_CLIENT", "params": {"kind": "bar"}}
    assert not isinstance(r.to_dict()["params"]["kind"], Enum)  # 不是 ChartType 实例


def test_to_dict_passthrough_params() -> None:
    """无 kind 的 code：params 原样透传（rows 是 int，不包装）。"""
    r = buildVisualRationale(ruleId="R02_SHARE_DONUT", kind=ChartType.DONUT, rowCount=3)
    assert r.to_dict() == {"code": "R02_SHARE_DONUT", "params": {"rows": 3}}


def test_to_dict_degrades_to_spec_invalid() -> None:
    """降级依据同样归一 kind。"""
    r = buildVisualRationale(
        ruleId="R07_TREND_LINE", kind=ChartType.LINE, rowCount=3,
        degradeReason="spec 校验失败",
    )
    assert r.to_dict() == {"code": "DEGRADE_SPEC_INVALID", "params": {"kind": "line"}}
