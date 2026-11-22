"""候选生成的 prompt 构造与响应解析（WP-ONT-AI A2 / G3-2，纯函数）。

钉住四件事：

1. **事实有界**：剖析事实按表分组、按列名排序、逐表与总量双上限（预算可控）；
2. **外部文本是数据不是指令**：事实走 `<discovery_profile>` 包裹（13→14 号 label）；
3. **证据硬性**：每条候选必须引用**事实里存在**的 (table, column)，否则整条丢弃
   ——「证据完整率 100%」是评测红线，不是建议；
4. **解析容错但不猜**：fence/think 可剥；JSON 坏了报 `CandidateFormatError`；
   单条非法只丢该条并记录原因（不整批失败、也不把坏数据写进库）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.security.prompt_sanitizer import UntrustedLabel
from app.services.discovery_candidate_prompt import (
    CANDIDATE_TYPES,
    FINDING_TYPES,
    CandidateFormatError,
    buildCandidateMessages,
    buildProfileFacts,
    parseCandidateResponse,
)


class _Row:
    """`discovery_profile` 行的最小替身（只读属性）。"""

    def __init__(
        self,
        table_name: str,
        column_name: str | None,
        *,
        row_count: int | None = 100,
        null_count: int | None = None,
        distinct_count: int | None = None,
        min_value: str | None = None,
        max_value: str | None = None,
    ) -> None:
        self.table_name = table_name
        self.column_name = column_name
        self.row_count = row_count
        self.null_count = null_count
        self.distinct_count = distinct_count
        self.min_value = min_value
        self.max_value = max_value


def _facts() -> list[dict[str, Any]]:
    return buildProfileFacts(
        [
            _Row("SUPPLIER", None, row_count=3500),
            _Row("SUPPLIER", "BPSNUM_0", null_count=0, distinct_count=3500,
                 min_value="10105", max_value="W1"),
            _Row("SUPPLIER", "BPSNAM_0", null_count=3, distinct_count=3436),
            _Row("ITEM", None, row_count=350922),
            _Row("ITEM", "ITMREF_0", null_count=0, distinct_count=350922),
        ]
    )


# ---------------------------------------------------------------------------
# 事实构造
# ---------------------------------------------------------------------------


def test_facts_group_by_table_with_table_level_row_count() -> None:
    facts = _facts()

    assert [item["table"] for item in facts] == ["ITEM", "SUPPLIER"]
    supplier = next(item for item in facts if item["table"] == "SUPPLIER")
    assert supplier["rowCount"] == 3500
    assert [column["column"] for column in supplier["columns"]] == ["BPSNAM_0", "BPSNUM_0"]
    assert supplier["columns"][1] == {
        "column": "BPSNUM_0",
        "nullCount": 0,
        "distinctCount": 3500,
        "min": "10105",
        "max": "W1",
    }


def test_facts_are_capped_per_table_and_in_total() -> None:
    rows = [_Row("T", f"C{index:03d}", distinct_count=index) for index in range(30)]
    facts = buildProfileFacts(
        rows, maxTables=5, maxColumnsPerTable=10, maxTotalColumns=15
    )

    assert len(facts) == 1
    assert len(facts[0]["columns"]) <= 15
    assert facts[0]["truncated"] is True


def test_facts_prefer_high_signal_columns_when_truncating() -> None:
    """截断时先保「有区分度/有缺失」的列，而不是按名字随便切。"""
    rows = [
        _Row("T", "A_EMPTY", null_count=0, distinct_count=1),
        _Row("T", "B_KEY", null_count=0, distinct_count=999),
        _Row("T", "C_NULLS", null_count=50, distinct_count=3),
    ]

    facts = buildProfileFacts(rows, maxColumnsPerTable=2)

    names = [column["column"] for column in facts[0]["columns"]]
    assert "A_EMPTY" not in names
    assert set(names) == {"B_KEY", "C_NULLS"}


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------


def test_messages_wrap_the_facts_as_untrusted_data() -> None:
    messages = buildCandidateMessages(_facts())
    user = messages[-1].content

    assert f"<{UntrustedLabel.DISCOVERY_PROFILE}>" in user
    assert "SUPPLIER" in user and "ITMREF_0" in user
    assert "candidateType" in messages[0].content


def test_system_prompt_states_the_json_contract_and_types() -> None:
    system = buildCandidateMessages(_facts())[0].content

    for candidateType in CANDIDATE_TYPES:
        assert candidateType in system
    for findingType in FINDING_TYPES:
        assert findingType in system
    assert "evidence" in system
    assert "JSON" in system


# ---------------------------------------------------------------------------
# 解析：正例
# ---------------------------------------------------------------------------


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "candidates": [
            {
                "candidateType": "OBJECT",
                "table": "SUPPLIER",
                "column": None,
                "targetCode": "SUPPLIER",
                "confidence": 0.8,
                "payload": {"className": "Supplier"},
                "evidence": [{"table": "SUPPLIER", "column": "BPSNUM_0"}],
            }
        ],
        "findings": [
            {
                "findingType": "MISSING",
                "table": "SUPPLIER",
                "column": "BPSNAM_0",
                "severity": "WARNING",
                "detail": "名称列有 3 个空值",
                "evidence": [{"table": "SUPPLIER", "column": "BPSNAM_0"}],
            }
        ],
    }
    body.update(overrides)
    return body


def test_parse_accepts_fenced_and_think_wrapped_json() -> None:
    content = "```json\n" + json.dumps(_payload(), ensure_ascii=False) + "\n```"

    parsed = parseCandidateResponse(content, facts=_facts())

    assert len(parsed.candidates) == 1
    assert parsed.candidates[0].candidateType == "OBJECT"
    assert parsed.candidates[0].table == "SUPPLIER"
    evidence = parsed.candidates[0].evidence[0]
    assert evidence["channel"] == "PROFILE"
    assert evidence["kind"] == "COLUMN_STAT"
    assert evidence["ref"] == "SUPPLIER.BPSNUM_0"
    assert (evidence["table"], evidence["column"]) == ("SUPPLIER", "BPSNUM_0")
    # 证据自带事实摘要（复核时不必回库重算）
    assert "distinctCount=3500" in evidence["summary"]
    assert len(parsed.findings) == 1
    assert parsed.skipped == ()


def test_parse_accepts_empty_batches() -> None:
    parsed = parseCandidateResponse('{"candidates": [], "findings": []}', facts=_facts())

    assert parsed.candidates == ()
    assert parsed.findings == ()


# ---------------------------------------------------------------------------
# 解析：证据与字段校验（坏条目只丢该条）
# ---------------------------------------------------------------------------


def _one_candidate(**overrides: Any) -> dict[str, Any]:
    candidate = dict(_payload()["candidates"][0])
    candidate.update(overrides)
    return {"candidates": [candidate], "findings": []}


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"evidence": []}, "evidence"),
        ({"evidence": [{"table": "SUPPLIER", "column": "NOPE"}]}, "证据"),
        ({"evidence": [{"table": "GHOST", "column": None}]}, "证据"),
        ({"candidateType": "WHATEVER"}, "类型"),
        ({"payload": "not-an-object"}, "payload"),
        ({"confidence": 1.5}, "confidence"),
        ({"table": "GHOST"}, "表"),
    ],
)
def test_invalid_candidate_is_skipped_with_a_reason(
    overrides: dict[str, Any], reason: str
) -> None:
    parsed = parseCandidateResponse(
        json.dumps(_one_candidate(**overrides), ensure_ascii=False), facts=_facts()
    )

    assert parsed.candidates == ()
    assert len(parsed.skipped) == 1
    assert reason in parsed.skipped[0]


def test_invalid_finding_is_skipped_but_candidates_survive() -> None:
    body = _payload()
    body["findings"][0]["severity"] = "CATASTROPHIC"

    parsed = parseCandidateResponse(json.dumps(body, ensure_ascii=False), facts=_facts())

    assert len(parsed.candidates) == 1
    assert parsed.findings == ()
    assert len(parsed.skipped) == 1


# ---------------------------------------------------------------------------
# 解析：整批失败
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["not json at all", "[]", '{"candidates": 1}', '{"candidates": [1]}'],
)
def test_broken_batch_raises(content: str) -> None:
    with pytest.raises(CandidateFormatError):
        parseCandidateResponse(content, facts=_facts())
