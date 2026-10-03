"""LLM 文本 → 结构 三条解析路径的 think 块隔离单测。

背景（2026-10-03 真机 400）：推理模型（MiniMax-M3）把思维链以
<think>…</think> 内联在回复**开头**，而内部解析器用「找第一个 ```` ``` ```` 围栏」
或「找第一个 ``{`` 」定位结构起点——think 块内部的示例 JSON / 围栏被当成了模型
真正的输出：

- ``_parsePlanOutcome``：``find("{")`` 命中 think 内的 JSON → 从中间截断 →
  JSONDecodeError → ``PLAN_REPLY_JSON_INVALID`` → HTTP 400
- ``parseSqlFromResponse``：``startswith(("SELECT","WITH"))`` 对 ``<think>`` 开头
  的内容不成立 → 返回 None → SQL 重试耗尽
- ``stripJsonFence``：``_JSON_FENCE_RE.search`` 命中 think 内的围栏 →
  **静默返回 think 里的错误数据**，无任何错误信号（最危险的一条）

本模块锁住三处入口的剥离行为，并固定一条刻意的行为变更：未闭合 ``<think>``
被剥离后候选变空 → 失败原因从 ``PLAN_REPLY_JSON_INVALID`` 变为更准确的
``PLAN_REPLY_EMPTY``（重试会重新问一次）。
"""

from __future__ import annotations

from app.services.llm_json_fence import stripJsonFence
from app.services.nl2sql_plan import (
    REASON_PLAN_REPLY_EMPTY,
    _parsePlanOutcome,
)
from app.services.nl2sql_service import Nl2SqlService

# ---------------------------------------------------------------------------
# 夹具：三种典型污染形态
# ---------------------------------------------------------------------------

# 形态 1：think 内含花括号（本次线上 400 的真身）
_THINK_WITH_BRACES = (
    "<think>The user wants per-site breakdown. "
    'Consider {"selectedClasses": ["WRONG_FROM_THINK"]} as a candidate. '
    "Now finalize.</think>\n"
)
# 形态 2：think 内含 ```json 围栏（命中 stripJsonFence 的静默错数据路径）
_THINK_WITH_FENCE = (
    "<think>Let me sketch: "
    '```json\n{"selectedClasses": ["WRONG_FROM_THINK"]}\n```\n'
    "That looks right.</think>\n"
)
# 形态 3：think 未闭合（模型漏写闭合标签）
_THINK_UNCLOSED = '<think>I am still reasoning about {"selectedClasses": ["X"]}'

_PLAN_JSON = (
    '{"interpretation":"按收货地点拆分",'
    '"selectedClasses":["DWD_GOODS_RECEIPT_DTL"],'
    '"conditions":["SUPPLIER_CODE = \'B019\'"],'
    '"aggregations":[{"property":"RCV_QTY_PUU","function":"SUM"}]}'
)
_FENCED_PLAN_JSON = f"```json\n{_PLAN_JSON}\n```"


def _svc() -> Nl2SqlService:
    """只调纯函数 parseSqlFromResponse，不需要构造完整服务依赖。"""
    return Nl2SqlService.__new__(Nl2SqlService)


class TestParsePlanOutcomeThinkIsolation:
    """路径 A：nl2sql_plan._parsePlanOutcome"""

    def test_braces_inside_think_do_not_hijack_json_start(self) -> None:
        """think 内的花括号不得成为 JSON 起点（线上 400 的根因）。"""
        outcome = _parsePlanOutcome(_THINK_WITH_BRACES + _PLAN_JSON)

        assert outcome.plan is not None
        assert outcome.plan.selectedClasses == ("DWD_GOODS_RECEIPT_DTL",)
        assert "WRONG_FROM_THINK" not in outcome.plan.selectedClasses

    def test_fenced_plan_after_think_parses(self) -> None:
        """think + ```json 围栏包裹的真实计划，应取围栏内内容。"""
        outcome = _parsePlanOutcome(_THINK_WITH_FENCE + _FENCED_PLAN_JSON)

        assert outcome.plan is not None
        assert outcome.plan.selectedClasses == ("DWD_GOODS_RECEIPT_DTL",)

    def test_unclosed_think_yields_empty_reply_reason(self) -> None:
        """未闭合 think 按「仍在思维链内」整体剥离 → 报 EMPTY 而非 JSON_INVALID。

        这是刻意的行为变更（更准确的失败原因，重试会重新问一次）。
        """
        outcome = _parsePlanOutcome(_THINK_UNCLOSED + _PLAN_JSON)

        assert outcome.plan is None
        assert outcome.reason == REASON_PLAN_REPLY_EMPTY

    def test_no_think_is_byte_identical_passthrough(self) -> None:
        """无 think 时行为与修复前完全一致（守约既有调用方）。"""
        outcome = _parsePlanOutcome(_PLAN_JSON)

        assert outcome.plan is not None
        assert outcome.plan.selectedClasses == ("DWD_GOODS_RECEIPT_DTL",)
        assert outcome.reason is None

    def test_conditions_only_plan_still_valid(self) -> None:
        """think 剥离不得放宽/收紧 _isEmptyPlan 口径：仅 conditions 仍算有效计划。"""
        outcome = _parsePlanOutcome(
            _THINK_WITH_BRACES + '{"conditions":["SUPPLIER_CODE = \'B019\'"]}'
        )

        assert outcome.plan is not None
        assert outcome.plan.conditions == ("SUPPLIER_CODE = 'B019'",)


class TestParseSqlFromResponseThinkIsolation:
    """路径 B：nl2sql_service.parseSqlFromResponse"""

    def test_bare_select_after_think_is_extracted(self) -> None:
        """think 前缀 + 裸 SELECT：修复前返回 None（SQL 阶段必然重试耗尽）。"""
        sql = _svc().parseSqlFromResponse(
            _THINK_WITH_BRACES + "SELECT RCV_SITE_CODE, SUM(RCV_QTY_PUU) FROM DWD_GOODS_RECEIPT_DTL"
        )

        assert sql is not None
        assert sql.startswith("SELECT RCV_SITE_CODE")

    def test_fenced_sql_after_think_is_extracted(self) -> None:
        """think + ```sql 围栏：取围栏内 SQL，且不是 think 内的内容。"""
        sql = _svc().parseSqlFromResponse(
            "<think>试试 ```sql\nSELECT 1 FROM DUAL\n``` </think>\n"
            "```sql\nSELECT RCV_SITE_CODE FROM DWD_GOODS_RECEIPT_DTL\n```"
        )

        assert sql == "SELECT RCV_SITE_CODE FROM DWD_GOODS_RECEIPT_DTL"

    def test_unclosed_think_returns_none_without_raising(self) -> None:
        """未闭合 think 整体剥离 → 无 SQL 可取，返回 None 且不抛错。"""
        sql = _svc().parseSqlFromResponse(
            _THINK_UNCLOSED + "SELECT RCV_SITE_CODE FROM DWD_GOODS_RECEIPT_DTL"
        )

        assert sql is None

    def test_no_think_behaviour_unchanged(self) -> None:
        """无 think：裸 SELECT 命中、非 SELECT 返回 None（守约）。"""
        svc = _svc()
        assert svc.parseSqlFromResponse("SELECT 1 FROM DUAL") == "SELECT 1 FROM DUAL"
        assert svc.parseSqlFromResponse("抱歉，我无法回答") is None


class TestStripJsonFenceThinkIsolation:
    """路径 C：llm_json_fence.stripJsonFence（静默错数据路径，最需要护住）"""

    def test_fence_inside_think_is_not_returned(self) -> None:
        """think 内的围栏不得被当成输出——修复前会静默返回 WRONG_FROM_THINK。"""
        result = stripJsonFence(_THINK_WITH_FENCE + _FENCED_PLAN_JSON)

        assert "WRONG_FROM_THINK" not in result
        assert "DWD_GOODS_RECEIPT_DTL" in result

    def test_think_only_fence_yields_empty(self) -> None:
        """think 内有围栏但其后无真实围栏：剥除 think 后无内容可抽。"""
        assert stripJsonFence(_THINK_WITH_FENCE).strip() == ""

    def test_think_without_fence_is_stripped(self) -> None:
        """think 无围栏但其后有裸 JSON：剥 think 后原样返回裸 JSON。"""
        assert stripJsonFence(_THINK_WITH_BRACES + _PLAN_JSON) == _PLAN_JSON

    def test_no_think_is_passthrough(self) -> None:
        """无 think：围栏剥离行为不变（守约 DQ 规则/假设/报告模板三处消费方）。"""
        assert stripJsonFence('```json\n{"a": 1}\n```') == '{"a": 1}'
        assert stripJsonFence('{"a": 1}') == '{"a": 1}'

    def test_empty_input_unchanged(self) -> None:
        assert stripJsonFence("") == ""