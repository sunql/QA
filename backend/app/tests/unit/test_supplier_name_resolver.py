"""SupplierNameResolver 单测（Phase 6.5 Task 2）。

Fake session 注入预设查询结果（不触真实 DB）；覆盖 resolve() 全部分支
与 apply() 替换语义。
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from app.domain.error_messages import (
    MSG_SUPPLIER_NAME_AMBIGUOUS,
    MSG_SUPPLIER_NAME_AMBIGUOUS_OVER_LIMIT,
    MSG_SUPPLIER_NAME_NOT_FOUND,
)
from app.domain.exceptions import ValidationError
from app.services.supplier_name_resolver import (
    _CANDIDATE_DISPLAY_LIMIT,
    ResolvedKey,
    SupplierNameResolver,
    _bareNameCandidates,
    _format_candidates,
    _rows_to_pairs,
)


class _FakeResult:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """按**语句特征**分派预设行（不按调用顺序）。

    原实现是「按顺序弹队列」，假设 resolve() 恰好发 2 次查询（exact → like）。
    2026-09-30 追加 Pass 2b（裸名候选集）后，这个假设失效：新增一次查询会让
    既有用例**取到下一个分支的结果** —— 表现为「实现没改错、测试却红了」的假象。
    故改为按 compiled SQL 分派：

    - `name ILIKE ...`   → likeRows
    - `name IN (...)`    → candidateRows（裸名候选集查询）
    - 其余（`name = ...`）→ exactRows
    """

    def __init__(
        self,
        exact_rows: list[tuple],
        like_rows: list[tuple],
        candidate_rows: list[tuple] | None = None,
    ):
        self._exact = list(exact_rows)
        self._like = list(like_rows)
        self._candidate = list(candidate_rows or [])
        self.executeCount = 0
        self.sqls: list[str] = []

    async def execute(self, stmt, *_args, **_kwargs):
        self.executeCount += 1
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        self.sqls.append(sql)
        if " ILIKE " in sql:
            return _FakeResult(self._like)
        if " IN " in sql:
            return _FakeResult(self._candidate)
        return _FakeResult(self._exact)


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


class TestBareNameLeftEdgeContract:
    """裸名左边界由**词典**裁决，不由正则猜（2026-09-30）。

    缺陷：中文无词间空格，`_BARE_NAME_RE` 的贪婪字符类会把紧邻的动词吞进公司名。
    真实问句「查询浙江力航汽车部件有限公司的采购订单」提取出的是
    「查询浙江力航汽车部件有限公司」→ 精确/LIKE 全落空 → 报「未找到该供应商」。
    实测 8 条真实问句 3 条失败，全部是「动词与公司名无分隔粘连」这一形态。

    修法：右边界（公司后缀）可靠，故把右边界固定的所有窗口交给 entity_mapping
    的 3500 个真实名字去裁决，取最长的精确命中。
    """

    _FULL = "浙江力航汽车部件有限公司"

    @pytest.mark.parametrize(
        "message",
        [
            f"查询{_FULL}的采购订单",
            f"我要看{_FULL}的360°视图",
            f"用{_FULL}重试",
            f"对比{_FULL}和济南吉利汽车有限公司的供货量",
        ],
    )
    def test_bare_name_glued_to_verb_resolves_via_dictionary(self, message):
        session = _FakeSession(
            exact_rows=[],
            like_rows=[],
            candidate_rows=[("B125", self._FULL)],
        )
        resolved = _run(SupplierNameResolver().resolve(message, session))
        assert resolved == ResolvedKey(
            key="B125", resolved_by="name_exact", original_name=self._FULL
        )

    def test_longest_dictionary_hit_wins_over_shorter_nested_name(self):
        """窗口是嵌套的（同一右边界向左扩展）；两者都真实存在时取覆盖更多原文的那个。"""
        session = _FakeSession(
            exact_rows=[],
            like_rows=[],
            candidate_rows=[
                ("10118", "力航汽车部件有限公司"),  # 短窗口也真实存在
                ("B125", self._FULL),  # 长窗口才是用户所指
            ],
        )
        resolved = _run(
            SupplierNameResolver().resolve(f"查询{self._FULL}的采购订单", session)
        )
        assert resolved.key == "B125"
        assert resolved.original_name == self._FULL

    def test_correct_bare_name_does_not_pay_extra_query(self):
        """反向守卫：裸名提取本就正确时（Pass 2 命中）不应多发候选集查询。

        这条钉住「修缺陷不能把热路径变慢」——Pass 2b 只在 Pass 2 落空后才跑。
        """
        session = _FakeSession(
            exact_rows=[("10105", "济南吉利汽车有限公司")], like_rows=[]
        )
        resolved = _run(
            SupplierNameResolver().resolve("济南吉利汽车有限公司 的情况", session)
        )
        assert resolved.resolved_by == "name_exact"
        assert session.executeCount == 1  # 只有 Pass 2 那一次

    def test_dictionary_miss_still_falls_through_to_like_then_not_found(self):
        """反向守卫：词典没有的「公司名」不得被候选集查询救活 —— 仍走 LIKE、仍报未找到。"""
        session = _FakeSession(exact_rows=[], like_rows=[], candidate_rows=[])
        with pytest.raises(ValidationError) as exc_info:
            _run(
                SupplierNameResolver().resolve(
                    "查询不存在的某某有限公司的采购订单", session
                )
            )
        assert MSG_SUPPLIER_NAME_NOT_FOUND.split("{name}")[0] in exc_info.value.message

    def test_nested_like_match_still_used_when_candidates_miss(self):
        """候选集查询有结果但不含最长窗口时，用最长命中而非候选集里的任意一条。"""
        session = _FakeSession(
            exact_rows=[],
            like_rows=[],
            candidate_rows=[("B125", self._FULL), ("77777", "航汽车部件有限公司")],
        )
        resolved = _run(
            SupplierNameResolver().resolve(f"我要看{self._FULL}的视图", session)
        )
        assert resolved.key == "B125"


class TestBareNameCandidates:
    """`_bareNameCandidates` 的窗口枚举契约。"""

    def test_windows_end_with_the_suffix_and_shrink_to_min_prefix(self):
        # 前缀「浙江力航汽车部件」8 字符 → 前缀长 8/7/6/5/4 共 5 个窗口
        candidates = _bareNameCandidates("浙江力航汽车部件有限公司")
        assert candidates[0] == "浙江力航汽车部件有限公司"  # 最长 = 原串
        assert candidates[-1] == "汽车部件有限公司"  # 最短 = 前缀恰 4 字符
        assert all(c.endswith("有限公司") for c in candidates)
        assert len(candidates) == len(set(candidates)) == 5

    def test_prefix_too_short_yields_no_candidate(self):
        # 「XX有限公司」前缀仅 2 字符 → 不可能由裸名路径合理产出
        assert _bareNameCandidates("XX有限公司") == ()

    def test_limited_liability_suffix_is_recognized(self):
        candidates = _bareNameCandidates("济南吉利汽车有限责任公司")
        assert candidates[0] == "济南吉利汽车有限责任公司"

    def test_name_without_company_suffix_yields_no_candidate(self):
        assert _bareNameCandidates("吉利汽车") == ()


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
