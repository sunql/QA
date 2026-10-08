"""think_block 模块单测（Think_Hide 系统参数 + 推理思维链剥离）。

stripThinkBlocks / ThinkStreamFilter 的判别用例覆盖：标签跨片切断、
未闭合块、无 think 时的字节级透传。isThinkHideEnabled 用假 session
覆盖真/假/缺席/异常四路（安全降级 = 不隐藏）。
"""

from __future__ import annotations

import pytest

from app.services.think_block import (
    THINK_HIDE_KEY,
    ThinkStreamFilter,
    applyThinkPolicy,
    isThinkHideEnabled,
    stripThinkBlocks,
)


class TestStripThinkBlocks:
    def test_no_think_tag_passthrough_byte_identical(self) -> None:
        text = "2026年4月的收货数量为 32,960,183。\n\n第二段。"
        assert stripThinkBlocks(text) == text

    def test_single_block_with_leading_whitespace_collapsed(self) -> None:
        assert stripThinkBlocks("<think>推理过程</think>\n\n  最终答案") == "最终答案"

    def test_multiple_blocks_removed(self) -> None:
        assert stripThinkBlocks("a<think>x</think>b<think>y</think>c") == "abc"

    def test_unclosed_block_hides_remainder(self) -> None:
        assert stripThinkBlocks("前文<think>推理没写完...") == "前文"

    def test_block_only(self) -> None:
        assert stripThinkBlocks("<think>只有思维链</think>") == ""

    def test_none_returns_empty(self) -> None:
        assert stripThinkBlocks(None) == ""

    def test_case_insensitive(self) -> None:
        assert stripThinkBlocks("<THINK>x</THINK>ok") == "ok"

    def test_multiline_reasoning(self) -> None:
        text = "<think>第一行\n第二行\n</think>\n\n答案"
        assert stripThinkBlocks(text) == "答案"


class TestThinkStreamFilter:
    def test_no_think_passthrough(self) -> None:
        f = ThinkStreamFilter()
        out = f.feed("你好") + f.feed("世界") + f.flush()
        assert out == "你好世界"

    def test_open_tag_split_across_chunks(self) -> None:
        f = ThinkStreamFilter()
        assert f.feed("你好<thi") == "你好"
        assert f.feed("nk>隐藏") == ""
        assert f.feed("</think>正文") == "正文"
        assert f.flush() == ""

    def test_close_tag_split_across_chunks(self) -> None:
        f = ThinkStreamFilter()
        assert f.feed("<think>abc</thi") == ""
        assert f.feed("nk>ok") == "ok"
        assert f.flush() == ""

    def test_unclosed_block_flush_hides_remainder(self) -> None:
        f = ThinkStreamFilter()
        assert f.feed("ok<think>xyz") == "ok"
        assert f.flush() == ""

    def test_multiple_blocks(self) -> None:
        f = ThinkStreamFilter()
        out = f.feed("a<think>x</think>b<think>y") + f.feed("</think>c") + f.flush()
        assert out == "abc"

    def test_unpaired_close_tag_is_plain_text(self) -> None:
        f = ThinkStreamFilter()
        out = f.feed("a</think>b") + f.flush()
        assert out == "a</think>b"

    def test_flush_emits_pending_candidate(self) -> None:
        """流以 '<' 截断收尾时，缓冲里的候选标签要吐出来，不能吞字。"""
        f = ThinkStreamFilter()
        assert f.feed("你好<") == "你好"
        assert f.flush() == "<"

    def test_stream_equals_full_strip(self) -> None:
        """流式逐片输出拼起来 == 全文剥离（两套实现的一致性契约）。"""
        full = "<think>reason\nspan</think>\n\n最终答案，含标点。"
        f = ThinkStreamFilter()
        pieces = [full[i:i + 3] for i in range(0, len(full), 3)]
        streamed = "".join(f.feed(p) for p in pieces) + f.flush()
        assert streamed == stripThinkBlocks(full)

    def test_double_lt_then_think(self) -> None:
        """NORMAL 态「<<」连写：第一个 '<' 是真实文本，第二个起才是标签。"""
        f = ThinkStreamFilter()
        out = f.feed("a<<think>b</think>c") + f.flush()
        assert out == "a<c"
        assert stripThinkBlocks("a<<think>b</think>c") == "a<c"

    def test_in_think_stray_close_prefix_junk(self) -> None:
        """think 内出现 '</thx' 这类断裂候选：全部丢弃，不影响后续闭合。"""
        f = ThinkStreamFilter()
        out = f.feed("a<think>x</thx y</think>ok") + f.flush()
        assert out == "aok"

    def test_whitespace_only_after_block_emits_nothing(self) -> None:
        """剥块后只剩空白：一个字都不下发（与全文 lstrip 对齐）。"""
        f = ThinkStreamFilter()
        out = f.feed("<think>x</think>\n\n") + f.flush()
        assert out == ""
        assert stripThinkBlocks("<think>x</think>\n\n") == ""


class _FakeResult:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> str | None:
        return self._value


class _FakeSession:
    def __init__(self, value: str | None = None, exc: Exception | None = None) -> None:
        self._value = value
        self._exc = exc

    async def execute(self, query: object) -> _FakeResult:
        if self._exc is not None:
            raise self._exc
        return _FakeResult(self._value)


class TestIsThinkHideEnabled:
    async def test_value_one_enables(self) -> None:
        assert await isThinkHideEnabled(_FakeSession("1")) is True

    async def test_value_zero_disables(self) -> None:
        assert await isThinkHideEnabled(_FakeSession("0")) is False

    async def test_missing_row_disables(self) -> None:
        assert await isThinkHideEnabled(_FakeSession(None)) is False

    async def test_empty_value_disables(self) -> None:
        assert await isThinkHideEnabled(_FakeSession("")) is False

    async def test_session_error_safely_disables(self) -> None:
        err = RuntimeError("db down")
        assert await isThinkHideEnabled(_FakeSession(exc=err)) is False

    async def test_numeric_value_two_enables(self) -> None:
        """非 0/1 数字按 int>0 口径（与 _readBoolConfig 一致）。"""
        assert await isThinkHideEnabled(_FakeSession("2")) is True

    async def test_illegal_value_disables(self) -> None:
        assert await isThinkHideEnabled(_FakeSession("abc")) is False

    async def test_key_name_is_user_specified(self) -> None:
        """参数名按用户要求逐字使用 Think_Hide（不做大小写改写）。"""
        assert THINK_HIDE_KEY == "Think_Hide"


@pytest.mark.parametrize("value,expected", [("true", True), ("yes", True), ("off", False)])
async def test_literal_conventions_match_repo_bool_config(value: str, expected: bool) -> None:
    """字面量口径与 _readBoolConfig（nl2sql_service.py:197-198）保持一致。"""
    assert await isThinkHideEnabled(_FakeSession(value)) is expected


class TestApplyThinkPolicy:
    async def test_enabled_strips(self) -> None:
        assert await applyThinkPolicy(_FakeSession("1"), _THINK_SAMPLE) == _STRIPPED_SAMPLE

    async def test_disabled_passthrough(self) -> None:
        assert await applyThinkPolicy(_FakeSession("0"), _THINK_SAMPLE) == _THINK_SAMPLE

    async def test_enabled_no_think_tag_passthrough(self) -> None:
        text = "普通答案，没有思维链。"
        assert await applyThinkPolicy(_FakeSession("1"), text) == text


_THINK_SAMPLE = "<think>推理</think>\n\n最终答案"
_STRIPPED_SAMPLE = "最终答案"
