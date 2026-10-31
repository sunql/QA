"""统一 ID 派生 / 反解单元测试（M0-P0.1）。

覆盖 `app.domain.id_mapping` 的纯函数契约：
- 派生格式 `obj:{business_object}:{external_id}`
- 去空白归一（`' supplier '` 与 `'supplier'` 落到同一个 ID）
- 分段禁 `':'`（格式分隔符，否则反解二义）
- 分段与总长上限对齐 `id_mapping` 列宽（32 / 128 / 128）
- 反解往返与非法输入拒绝
- 控制字符 / 零宽字符拒绝，Unicode NFC 归一
"""
from __future__ import annotations

import pytest

from app.domain.exceptions import ValidationError
from app.domain.id_mapping import (
    UNIFIED_ID_MAX_LENGTH,
    buildUnifiedId,
    parseUnifiedId,
)

# 用 chr() 构造不可见字符：源码里不出现零宽字形，肉眼可审查。
ZERO_WIDTH_SPACE = chr(0x200B)
ZERO_WIDTH_NON_JOINER = chr(0x200C)
ZERO_WIDTH_JOINER = chr(0x200D)
BYTE_ORDER_MARK = chr(0xFEFF)
WORD_JOINER = chr(0x2060)
SOFT_HYPHEN = chr(0x00AD)
RIGHT_TO_LEFT_OVERRIDE = chr(0x202E)
LEFT_TO_RIGHT_MARK = chr(0x200E)
LONE_SURROGATE = chr(0xD800)
LINE_SEPARATOR = chr(0x2028)
COMBINING_ACUTE = chr(0x0301)
LATIN_SMALL_E_ACUTE = chr(0x00E9)


def test_build_unified_id_derives_from_two_segments() -> None:
    assert buildUnifiedId("supplier", "S001") == "obj:supplier:S001"


def test_build_unified_id_trims_whitespace() -> None:
    assert buildUnifiedId("  supplier ", " S001  ") == "obj:supplier:S001"


@pytest.mark.parametrize(
    "businessObject, externalId", [("", "S001"), ("supplier", ""), ("   ", "S001")]
)
def test_build_unified_id_rejects_empty_segment(
    businessObject: str, externalId: str
) -> None:
    with pytest.raises(ValidationError):
        buildUnifiedId(businessObject, externalId)


@pytest.mark.parametrize(
    "businessObject, externalId", [("sup:plier", "S1"), ("supplier", "S:1")]
)
def test_build_unified_id_rejects_separator_in_segment(
    businessObject: str, externalId: str
) -> None:
    with pytest.raises(ValidationError):
        buildUnifiedId(businessObject, externalId)


def test_build_unified_id_rejects_overlong_business_object() -> None:
    with pytest.raises(ValidationError):
        buildUnifiedId("b" * 33, "S001")


def test_build_unified_id_rejects_overlong_external_id() -> None:
    with pytest.raises(ValidationError):
        buildUnifiedId("supplier", "e" * 129)


def test_build_unified_id_rejects_result_over_column_width() -> None:
    """两段各自合法，但拼起来超过主键列宽（String(128)）。"""
    businessObject = "b" * 32
    externalId = "e" * 100  # 4 + 32 + 1 + 100 = 137 > 128
    assert 4 + len(businessObject) + 1 + len(externalId) > UNIFIED_ID_MAX_LENGTH
    with pytest.raises(ValidationError):
        buildUnifiedId(businessObject, externalId)


def test_build_unified_id_accepts_exact_column_width() -> None:
    businessObject = "b" * 32
    externalId = "e" * (UNIFIED_ID_MAX_LENGTH - 4 - 32 - 1)
    unifiedId = buildUnifiedId(businessObject, externalId)
    assert len(unifiedId) == UNIFIED_ID_MAX_LENGTH


@pytest.mark.parametrize(
    "badExternalId",
    [
        pytest.param("S1\x00", id="nul"),  # asyncpg 直接拒绝 → 500
        pytest.param("S\n1", id="embedded-newline"),  # 日志可伪造
        pytest.param("S\t1", id="embedded-tab"),
        pytest.param(f"S1{ZERO_WIDTH_SPACE}", id="zwsp"),
        pytest.param(f"S1{ZERO_WIDTH_NON_JOINER}", id="zwnj"),
        pytest.param(f"S1{ZERO_WIDTH_JOINER}", id="zwj"),
        pytest.param(f"S1{BYTE_ORDER_MARK}", id="bom"),
        pytest.param(f"S1{WORD_JOINER}", id="word-joiner"),
        pytest.param(f"S1{SOFT_HYPHEN}", id="soft-hyphen"),
        pytest.param(f"S1{RIGHT_TO_LEFT_OVERRIDE}", id="rtl-override"),
        pytest.param(f"S1{LEFT_TO_RIGHT_MARK}", id="lrm"),
        pytest.param(f"S1{LONE_SURROGATE}", id="lone-surrogate"),
        pytest.param(f"S1{LINE_SEPARATOR}2", id="embedded-line-separator"),
    ],
)
def test_build_unified_id_rejects_control_or_invisible_chars(badExternalId: str) -> None:
    """控制 / 格式 / 代理 / 分隔符类字符：DB 会炸 500、日志可伪造、肉眼同形。"""
    with pytest.raises(ValidationError):
        buildUnifiedId("supplier", badExternalId)


def test_build_unified_id_rejects_control_chars_in_business_object() -> None:
    with pytest.raises(ValidationError):
        buildUnifiedId("sup\nplier", "S1")


def test_build_unified_id_trims_trailing_newline() -> None:
    """首尾空白（含换行/制表符）属于可安全归一的输入，不算非法字符。"""
    assert buildUnifiedId("supplier\n", "S1\t") == "obj:supplier:S1"


def test_build_unified_id_normalizes_unicode_nfc() -> None:
    """é（U+00E9）与 e+组合重音（U+0065 U+0301）必须落到同一个 ID。"""
    composed = LATIN_SMALL_E_ACUTE
    decomposed = "e" + COMBINING_ACUTE
    assert buildUnifiedId("supplier", composed) == buildUnifiedId("supplier", decomposed)


def test_parse_unified_id_round_trips() -> None:
    unifiedId = buildUnifiedId("supplier", "S001")
    assert parseUnifiedId(unifiedId) == ("supplier", "S001")


@pytest.mark.parametrize(
    "unifiedId",
    [
        "",
        "obj:supplier",
        "obj:supplier:S001:extra",
        "x:supplier:S001",
        "obj::S001",
        "obj:supplier:",
    ],
)
def test_parse_unified_id_rejects_malformed(unifiedId: str) -> None:
    with pytest.raises(ValidationError):
        parseUnifiedId(unifiedId)


def test_parse_unified_id_rejects_control_chars() -> None:
    with pytest.raises(ValidationError):
        parseUnifiedId("obj:supplier:S1\x00")


def test_parse_unified_id_rejects_invisible_chars() -> None:
    with pytest.raises(ValidationError):
        parseUnifiedId(f"obj:supplier:S1{SOFT_HYPHEN}")


def test_parse_unified_id_rejects_overlong_segments() -> None:
    """反解与派生同一套宽度契约：外部输入不能绕过列宽。"""
    with pytest.raises(ValidationError):
        parseUnifiedId("obj:" + "b" * 33 + ":S1")
    with pytest.raises(ValidationError):
        parseUnifiedId("obj:supplier:" + "e" * 129)
