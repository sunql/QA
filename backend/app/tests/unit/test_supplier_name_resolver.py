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
