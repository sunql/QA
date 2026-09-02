"""SupplierNameResolver 单测（Phase 6.5 Task 2）。

Fake session 注入预设查询结果（不触真实 DB）；覆盖 resolve() 全部分支
与 apply() 替换语义。
"""

from __future__ import annotations

import pytest

from app.domain.error_messages import (
    MSG_SUPPLIER_NAME_AMBIGUOUS,
    MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT,
    MSG_SUPPLIER_NAME_NOT_FOUND,
)
from app.domain.exceptions import ValidationError
from app.services.supplier_name_resolver import (
    _CANDIDATE_DISPLAY_LIMIT,
    _format_candidates,
    _rows_to_pairs,
    ResolvedKey,
    SupplierNameResolver,
)


class _FakeResult:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """按 WHERE 子句顺序返回预设行：每次 execute 弹出队列首元素。

    resolve() 的查询顺序：exact（== name）→ like（ilike %name%）。
    """

    def __init__(self, exact_rows: list[tuple], like_rows: list[tuple]):
        self._queue = [_FakeResult(exact_rows), _FakeResult(like_rows)]
        self.executeCount = 0

    async def execute(self, *_args, **_kwargs):
        self.executeCount += 1
        return self._queue.pop(0)


class TestResolve:
    def test_numeric_code_short_circuits_without_db(self):
        session = _FakeSession(exact_rows=[], like_rows=[])
        resolved = _run(SupplierNameResolver().resolve("评估供应商 10105 的风险", session))
        assert resolved == ResolvedKey(
            key="10105", resolved_by="code_regex", original_name=None
        )
        assert session.executeCount == 0  # 数字路径零 DB 查询

    def test_no_supplier_keyword_returns_none(self):
        session = _FakeSession(exact_rows=[], like_rows=[])
        resolved = _run(SupplierNameResolver().resolve("今天天气如何", session))
        assert resolved is None
        assert session.executeCount == 0

    def test_exact_single_match(self):
        session = _FakeSession(
            exact_rows=[("10105", "济南吉利汽车有限公司")], like_rows=[]
        )
        resolved = _run(
            SupplierNameResolver().resolve(
                "供应商 济南吉利汽车有限公司 的 360° 视图", session
            )
        )
        assert resolved == ResolvedKey(
            key="10105", resolved_by="name_exact",
            original_name="济南吉利汽车有限公司",
        )

    def test_exact_multiple_raises_ambiguous_with_candidates(self):
        rows = [("10105", "吉利一厂"), ("10106", "吉利二厂")]
        session = _FakeSession(exact_rows=rows, like_rows=[])
        with pytest.raises(ValidationError) as exc_info:
            _run(
                SupplierNameResolver().resolve("供应商 吉利一厂", session)
            )
        assert exc_info.value.details == {
            "candidates": [["10105", "吉利一厂"], ["10106", "吉利二厂"]]
        }
        assert "10105" in exc_info.value.message
        assert "2" in exc_info.value.message

    def test_like_unique_match(self):
        session = _FakeSession(
            exact_rows=[], like_rows=[("10111", "宁波泰鸿机电有限公司")]
        )
        resolved = _run(SupplierNameResolver().resolve("供应商 泰鸿机电", session))
        assert resolved == ResolvedKey(
            key="10111", resolved_by="name_like", original_name="泰鸿机电"
        )

    def test_like_multiple_raises_ambiguous(self):
        rows = [("10105", "济南吉利汽车有限公司"), ("10118", "宁波吉利汽车研究开发有限公司")]
        session = _FakeSession(exact_rows=[], like_rows=rows)
        with pytest.raises(ValidationError) as exc_info:
            _run(SupplierNameResolver().resolve("供应商 吉利汽车", session))
        assert len(exc_info.value.details["candidates"]) == 2
        assert MSG_SUPPLIER_NAME_AMBIGUOUS.split("{name}")[0] in exc_info.value.message

    def test_like_over_limit_raises_over_limit_without_listing(self):
        rows = [(str(10000 + i), f"汽车供应商{i}") for i in range(_CANDIDATE_DISPLAY_LIMIT)]
        session = _FakeSession(exact_rows=[], like_rows=rows)
        with pytest.raises(ValidationError) as exc_info:
            _run(SupplierNameResolver().resolve("供应商 汽车", session))
        assert exc_info.value.details == {
            "candidate_count": _CANDIDATE_DISPLAY_LIMIT, "name": "汽车"
        }
        assert "过多" in exc_info.value.message
        assert "10105" not in exc_info.value.message  # 不列全量

    def test_not_found_raises(self):
        session = _FakeSession(exact_rows=[], like_rows=[])
        with pytest.raises(ValidationError) as exc_info:
            _run(SupplierNameResolver().resolve("供应商 不存在的公司", session))
        assert MSG_SUPPLIER_NAME_NOT_FOUND.split("{name}")[0] in exc_info.value.message
        assert exc_info.value.details is None


class TestRegexDelimiterContract:
    """Phase 6.5 Task 6 追加：name 提取正则的分隔符/虚词契约（防止普通问法被误解析为公司名）。"""

    def test_compound_noun_without_delimiter_returns_none(self):
        """「供应商采购额趋势」中「供应商」是名词修饰语，不是名称前缀 → 不提取。"""
        session = _FakeSession(exact_rows=[], like_rows=[])
        resolved = _run(
            SupplierNameResolver().resolve("查询供应商采购额趋势", session)
        )
        assert resolved is None
        assert session.executeCount == 0

    def test_end_of_string_name_with_space_still_extracted(self):
        """名称位于句末且无后续分隔符时，仍能正确提取。"""
        session = _FakeSession(
            exact_rows=[("10105", "济南吉利汽车有限公司")], like_rows=[]
        )
        resolved = _run(
            SupplierNameResolver().resolve("查询供应商 济南吉利汽车有限公司", session)
        )
        assert resolved == ResolvedKey(
            key="10105",
            resolved_by="name_exact",
            original_name="济南吉利汽车有限公司",
        )

    @pytest.mark.parametrize(
        "message",
        [
            "供应商:济南吉利汽车有限公司",
            "供应商：济南吉利汽车有限公司",
            "supplier 济南吉利汽车有限公司",
        ],
    )
    def test_colon_and_supplier_delimiter_variants_extract_name(self, message):
        """冒号（半角/全角）及 supplier 关键字后接空格均应正确提取名称。"""
        session = _FakeSession(
            exact_rows=[("10105", "济南吉利汽车有限公司")], like_rows=[]
        )
        resolved = _run(SupplierNameResolver().resolve(message, session))
        assert resolved == ResolvedKey(
            key="10105",
            resolved_by="name_exact",
            original_name="济南吉利汽车有限公司",
        )

    def test_particle_led_phrase_returns_none(self):
        """「供应商 的收货量怎么样」中「的」是虚词，不应开始名称提取。"""
        session = _FakeSession(exact_rows=[], like_rows=[])
        resolved = _run(
            SupplierNameResolver().resolve("供应商 的收货量怎么样", session)
        )
        assert resolved is None
        assert session.executeCount == 0

    def test_numeric_path_without_space_short_circuits(self):
        """数字编码与「供应商」之间无空格时，仍走数字正则短路，不触发 name 提取。"""
        session = _FakeSession(exact_rows=[], like_rows=[])
        resolved = _run(
            SupplierNameResolver().resolve("评估供应商10105的风险", session)
        )
        assert resolved == ResolvedKey(
            key="10105", resolved_by="code_regex", original_name=None
        )
        assert session.executeCount == 0


class TestBareCompanyNameContract:
    """无「供应商」前缀的裸公司名（带公司后缀）也可解析（后续优化落地）。

    用户实际输入形如「济南吉利汽车有限公司 的情况」——不带前缀。
    白名单后缀（有限公司/有限责任公司，真实数据 2743/3500 覆盖）把
    误报率控制在可接受范围：普通中文句子不会恰好以公司后缀结尾。
    """

    def test_bare_name_with_suffix_resolves(self):
        session = _FakeSession(
            exact_rows=[("10105", "济南吉利汽车有限公司")], like_rows=[]
        )
        resolved = _run(
            SupplierNameResolver().resolve("济南吉利汽车有限公司 的情况", session)
        )
        assert resolved == ResolvedKey(
            key="10105",
            resolved_by="name_exact",
            original_name="济南吉利汽车有限公司",
        )

    def test_bare_name_mid_sentence_resolves(self):
        session = _FakeSession(
            exact_rows=[("10105", "济南吉利汽车有限公司")], like_rows=[]
        )
        resolved = _run(
            SupplierNameResolver().resolve("查询 济南吉利汽车有限公司 的 360° 视图", session)
        )
        assert resolved == ResolvedKey(
            key="10105",
            resolved_by="name_exact",
            original_name="济南吉利汽车有限公司",
        )

    def test_bare_name_no_suffix_still_requires_prefix(self):
        """无后缀裸词（如「吉利」）不触发解析——误报率不可控。"""
        session = _FakeSession(exact_rows=[], like_rows=[])
        resolved = _run(SupplierNameResolver().resolve("查询 吉利 的情况", session))
        assert resolved is None
        assert session.executeCount == 0

    def test_suffix_alone_not_extracted(self):
        """「有限公司」本身不足以成为公司名——要求后缀前至少 2 个字符。"""
        session = _FakeSession(exact_rows=[], like_rows=[])
        resolved = _run(SupplierNameResolver().resolve("什么是有限公司", session))
        assert resolved is None
        assert session.executeCount == 0

    def test_bare_name_prefixed_without_delimiter_now_resolves(self):
        """「供应商{名称}」无分隔符写法（收紧正则后的已知缺口）经裸名路径恢复。"""
        session = _FakeSession(
            exact_rows=[("10105", "济南吉利汽车有限公司")], like_rows=[]
        )
        resolved = _run(
            SupplierNameResolver().resolve("评估供应商济南吉利汽车有限公司的风险", session)
        )
        assert resolved == ResolvedKey(
            key="10105",
            resolved_by="name_exact",
            original_name="济南吉利汽车有限公司",
        )

    def test_bare_name_ambiguous_raises_with_candidates(self):
        rows = [("10105", "济南吉利汽车有限公司"), ("10118", "济南吉利汽车研究开发有限公司")]
        session = _FakeSession(exact_rows=[], like_rows=rows)
        with pytest.raises(ValidationError) as exc_info:
            _run(
                SupplierNameResolver().resolve(
                    "济南吉利汽车有限公司 情况不明", session
                )
            )
        assert len(exc_info.value.details["candidates"]) == 2


class TestApply:
    def test_none_resolved_returns_original(self):
        r = SupplierNameResolver()
        assert r.apply("评估供应商 10105", None) == "评估供应商 10105"

    def test_code_regex_returns_original(self):
        r = SupplierNameResolver()
        resolved = ResolvedKey(key="10105", resolved_by="code_regex", original_name=None)
        assert r.apply("评估供应商 10105 的风险", resolved) == "评估供应商 10105 的风险"

    def test_name_path_replaces_once(self):
        r = SupplierNameResolver()
        resolved = ResolvedKey(
            key="10105", resolved_by="name_exact",
            original_name="济南吉利汽车有限公司",
        )
        assert (
            r.apply("评估供应商 济南吉利汽车有限公司 的风险", resolved)
            == "评估供应商 10105 的风险"
        )

    def test_bare_name_replaced_with_canonical_prefix(self):
        """裸名（前面没有「供应商」前缀）替换为规范形态「供应商 {code}」，
        保证下游 arg_extractor 的数字正则可命中。"""
        r = SupplierNameResolver()
        resolved = ResolvedKey(
            key="10105", resolved_by="name_exact",
            original_name="济南吉利汽车有限公司",
        )
        assert r.apply("济南吉利汽车有限公司 的情况", resolved) == "供应商 10105 的情况"
        assert (
            r.apply("查询 济南吉利汽车有限公司 的 360° 视图", resolved)
            == "查询 供应商 10105 的 360° 视图"
        )

    def test_name_directly_after_prefix_replaced_with_bare_key(self):
        """名称前紧邻「供应商」（无/有分隔符）时只替换为编码，避免「供应商 供应商 10105」。"""
        r = SupplierNameResolver()
        resolved = ResolvedKey(
            key="10105", resolved_by="name_exact",
            original_name="济南吉利汽车有限公司",
        )
        assert (
            r.apply("评估供应商济南吉利汽车有限公司的风险", resolved)
            == "评估供应商10105的风险"
        )
        assert (
            r.apply("供应商：济南吉利汽车有限公司", resolved)
            == "供应商：10105"
        )


class TestHelpers:
    def test_format_candidates(self):
        assert (
            _format_candidates([("10105", "甲公司"), ("10106", "乙公司")])
            == "10105 甲公司 | 10106 乙公司"
        )

    def test_rows_to_pairs(self):
        assert _rows_to_pairs([("10105", "甲公司")]) == [["10105", "甲公司"]]


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)
