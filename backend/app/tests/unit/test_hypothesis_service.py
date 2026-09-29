"""v3.1 B6（M7 Hypothesis Hook）unit 测试：纯函数层。

覆盖：
- isHypothesisTrigger：词表全命中 + 正常 QUERY 问题不误伤（触发第二前提
  「本轮有数据」在 mixin 集成层验证，纯函数只看问题文本）
- parseHypotheses：fence 剥离 / 畸形 JSON / 畸形项丢弃 / 超 3 条裁剪 /
  因果句式容错（丢弃）/ 驼峰与下划线键兼容
- isValidVerificationSql：SELECT/WITH 放行，DML/DDL 关键词黑名单拦截
- stripJsonFence（llm_json_fence SSOT）：fence 优先、裸 JSON 原样
"""

from __future__ import annotations

import json

from app.services.hypothesis_service import (
    HYPOTHESIS_MAX_COUNT,
    HYPOTHESIS_TRIGGER_PHRASES,
    Hypothesis,
    isHypothesisTrigger,
    isValidVerificationSql,
    parseHypotheses,
)
from app.services.llm_json_fence import stripJsonFence


def _hypo_item(statement: str = "收货量下降可能与供应商交付延期有关",
               sql: str = "SELECT 1", driver: str | None = None) -> dict:
    item: dict = {"statement": statement, "verification_sql": sql}
    if driver is not None:
        item["driver"] = driver
    return item


class TestHypothesisTrigger:
    def test_every_phrase_in_wordlist_hits(self) -> None:
        """词表每个短语嵌进自然问法都必须命中（词表扩充防呆）。"""
        assert len(HYPOTHESIS_TRIGGER_PHRASES) >= 8, "词表至少 8 个短语"
        for phrase in HYPOTHESIS_TRIGGER_PHRASES:
            assert isHypothesisTrigger(f"本月收货量{phrase}，请看数据"), phrase

    def test_english_why_triggers(self) -> None:
        assert isHypothesisTrigger("why did the qty drop")
        assert isHypothesisTrigger("Why 下降")

    def test_why_inside_word_does_not_trigger(self) -> None:
        # "why" 只作独立词命中，不误伤恰好含 why 子串的词
        assert not isHypothesisTrigger("查询whyhigh订单")

    def test_normal_queries_do_not_trigger(self) -> None:
        for question in (
            "各供应商的收货数量汇总",
            "查询3月份采购订单数量",
            "按数量降序排序",
            "4月份呢",
            "你好",
            "",
            "   ",
        ):
            assert not isHypothesisTrigger(question), question

    def test_non_string_input_is_safe(self) -> None:
        assert not isHypothesisTrigger(None)  # type: ignore[arg-type]


class TestIsValidVerificationSql:
    def test_select_and_with_allowed(self) -> None:
        assert isValidVerificationSql("SELECT NAME FROM T WHERE QTY > 0")
        assert isValidVerificationSql("select name from t")
        assert isValidVerificationSql("WITH x AS (SELECT 1) SELECT * FROM x")
        assert isValidVerificationSql("  SELECT 1; ")

    def test_dml_ddl_blocked(self) -> None:
        for sql in (
            "DELETE FROM T",
            "UPDATE T SET QTY = 0",
            "INSERT INTO T VALUES (1)",
            "DROP TABLE T",
            "ALTER TABLE T ADD C INT",
            "CREATE TABLE T (ID INT)",
            "TRUNCATE TABLE T",
            "GRANT ALL ON T TO u",
            "MERGE INTO T USING S ON 1=1",
            "EXEC sp_x",
            "CALL proc()",
            "EXPLAIN SELECT 1",
        ):
            assert not isValidVerificationSql(sql), sql

    def test_substring_mentions_are_not_blocked(self) -> None:
        # 词边界：列名/表名里出现关键词子串不应误杀（update_time、sort_order）
        assert isValidVerificationSql("SELECT UPDATE_TIME, SORT_ORDER FROM T")

    def test_empty_is_invalid(self) -> None:
        assert not isValidVerificationSql("")
        assert not isValidVerificationSql("   ")
        assert not isValidVerificationSql(None)  # type: ignore[arg-type]


class TestParseHypotheses:
    def test_parse_plain_json_list(self) -> None:
        raw = json.dumps([
            _hypo_item("假设一", "SELECT A FROM T", "QTY"),
            _hypo_item("假设二", "SELECT B FROM T"),
        ], ensure_ascii=False)
        result = parseHypotheses(raw)
        assert len(result) == 2
        assert isinstance(result[0], Hypothesis)
        assert result[0].statement == "假设一"
        assert result[0].driver == "QTY"
        assert result[0].verificationSql == "SELECT A FROM T"
        assert result[1].driver is None

    def test_parse_strips_json_fence(self) -> None:
        raw = "```json\n" + json.dumps([_hypo_item()], ensure_ascii=False) + "\n```"
        assert len(parseHypotheses(raw)) == 1

    def test_parse_malformed_json_returns_empty(self) -> None:
        assert parseHypotheses("这不是 JSON") == []
        assert parseHypotheses("") == []

    def test_parse_non_list_returns_empty(self) -> None:
        assert parseHypotheses(json.dumps({"a": 1})) == []
        assert parseHypotheses('"hello"') == []

    def test_dict_wrapper_with_hypotheses_key(self) -> None:
        raw = json.dumps({"hypotheses": [_hypo_item()]}, ensure_ascii=False)
        assert len(parseHypotheses(raw)) == 1

    def test_malformed_items_dropped(self) -> None:
        raw = json.dumps([
            _hypo_item(),
            "not-a-dict",
            {"statement": "", "verification_sql": "SELECT 1"},  # 空 statement
            {"statement": "缺 SQL"},
            {"verification_sql": "SELECT 1"},  # 缺 statement
            _hypo_item(statement="   "),
        ], ensure_ascii=False)
        result = parseHypotheses(raw)
        assert len(result) == 1
        assert result[0].statement.startswith("收货量")

    def test_caps_to_max_count(self) -> None:
        raw = json.dumps(
            [_hypo_item(statement=f"假设{i}", sql="SELECT 1") for i in range(6)],
            ensure_ascii=False,
        )
        result = parseHypotheses(raw)
        assert len(result) == HYPOTHESIS_MAX_COUNT

    def test_causal_statement_dropped(self) -> None:
        raw = json.dumps([
            _hypo_item(statement="因为供应商延期所以收货下降", sql="SELECT 1"),
            _hypo_item(statement="收货下降可能与交付延期有关", sql="SELECT 2"),
        ], ensure_ascii=False)
        result = parseHypotheses(raw)
        assert len(result) == 1
        assert "可能" in result[0].statement

    def test_dml_verification_sql_dropped(self) -> None:
        raw = json.dumps([
            _hypo_item("合法", "SELECT 1"),
            _hypo_item("非法", "DELETE FROM T"),
        ], ensure_ascii=False)
        result = parseHypotheses(raw)
        assert [h.statement for h in result] == ["合法"]

    def test_camel_case_key_accepted(self) -> None:
        raw = json.dumps([
            {"statement": "s", "verificationSql": "SELECT 1", "driver": "QTY"},
        ])
        result = parseHypotheses(raw)
        assert len(result) == 1
        assert result[0].verificationSql == "SELECT 1"

    def test_driver_empty_string_normalized_to_none(self) -> None:
        raw = json.dumps([_hypo_item("s", "SELECT 1", driver="")])
        assert parseHypotheses(raw)[0].driver is None


class TestStripJsonFence:
    def test_fence_stripped(self) -> None:
        assert stripJsonFence('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_bare_json_untouched(self) -> None:
        assert stripJsonFence('{"a": 1}') == '{"a": 1}'

    def test_empty_input_untouched(self) -> None:
        assert stripJsonFence("") == ""
