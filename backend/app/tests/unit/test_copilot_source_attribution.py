"""采购 Copilot「来源表述」契约守卫（2026-11-26）。

**问题**：LLM 在给用户的结论里直接印工具名/参数名（`delivery_tracking_query`、
`window.mode=anchorDay`），业务方看不懂那是什么"数据来源"
（实测：83 条成功 run 里 68 条把工具名写进正文）。

**本文件钉住三件事**（都是**行为**层面的，不是文案形状）：

1. **种子指令不再要求写工具名**，且**明确禁止**正文出现内部术语
   —— 指令是 LLM 唯一的业务约束来源，措辞回退就会复发；
2. **编排系统提示词（全局、所有 agent 生效）也带同一条禁令**
   —— 只改种子指令对「非种子 Agent」（如 SUPPLIER_RISK_AGENT）无效；
3. **工具 answer 自带业务口径来源句**（数据来源体系 + 时间窗）
   —— LLM 抄 answer 里的说法，比凭空构造「来源」更可靠。
"""

from __future__ import annotations

from app.services.agent_orchestration_planner import _PLANNER_RULES
from app.services.delivery_tracking_query import DATA_LAYER_LABEL, _buildAnswer
from app.services.pending_delivery_query import _buildPendingToolResult
from scripts.seed_procurement_agent_profiles import (
    _LEGACY_READONLY_BASE,
    _READONLY_BASE,
    PROFILE_SEEDS,
)

#: 正文里禁止出现的内部术语样例（测试只查这几个代表，避免把正常词也禁掉）。
_INTERNAL_TERMS = (
    "delivery_tracking_query",
    "pending_delivery_query",
    "window.mode",
    "datasourceId",
)


class TestSeedInstructions:
    def test_readonly_base_does_not_ask_for_tool_name(self) -> None:
        """第 2 条不再要求「带上来源（工具名 + …）」。"""
        assert "工具名 +" not in _READONLY_BASE
        assert "（工具名 +" not in _READONLY_BASE

    def test_readonly_base_forbids_internal_terms(self) -> None:
        """明确禁止正文出现工具名/参数名/字段名。"""
        assert "内部术语" in _READONLY_BASE
        for term in ("delivery_tracking_query", "monthsBack", "window.mode"):
            assert term in _READONLY_BASE, f"禁令里没点名 {term}，LLM 不会当真"

    def test_every_profile_instruction_forbids_internal_terms(self) -> None:
        """每个采购场景 Agent 的指令都带禁令（不只公共块生效）。"""
        offenders = [
            code
            for code, (instructions, _tools, _it, _tc) in PROFILE_SEEDS.items()
            if "内部术语" not in instructions
        ]
        assert not offenders, f"这些 Agent 的指令没带内部术语禁令：{offenders}"

    def test_legacy_base_is_different_from_current(self) -> None:
        """上一版公共块必须与新版不同（否则同步逻辑无从判断「是否旧措辞」）。"""
        assert _LEGACY_READONLY_BASE != _READONLY_BASE
        # 旧版正是「要求写工具名」的那版 —— 存量库里的指令就是它
        assert "工具名 +" in _LEGACY_READONLY_BASE


class TestPlannerRules:
    def test_planner_rules_forbid_internal_identifiers(self) -> None:
        """全局规划器规则带同一条禁令（覆盖非种子 Agent）。"""
        lowered = _PLANNER_RULES.lower()
        assert "never mention tool names" in lowered
        assert "window.mode" in _PLANNER_RULES


class TestToolAnswers:
    def test_delivery_tracking_answer_cites_business_source(self) -> None:
        """到货跟踪 answer 自带「采购订单与到货记录 + 时间窗」的业务口径。"""
        from app.services.delivery_tracking_query import DeliveryTrackingWindow

        answer = _buildAnswer(
            {
                "orderArrivalRate": 83.67,
                "allArrivedOrders": 4052,
                "partialArrivedOrders": 434,
                "notArrivedOrders": 357,
                "totalOrders": 4843,
                "qtyArrivalRate": 46.73,
                "excludedRows": 6,
            },
            DeliveryTrackingWindow(
                dateFrom=__import__("datetime").date(2026, 10, 24),
                dateTo=__import__("datetime").date(2026, 11, 24),
                mode="anchorDay",
            ),
        )
        assert "采购订单与到货记录" in answer
        assert "2026-10-24" in answer and "2026-11-24" in answer

    def test_pending_delivery_answer_cites_business_source(self) -> None:
        """待到货 answer 同样自带业务口径来源句。"""
        result = _buildPendingToolResult(
            datasourceId=1,
            monthsBack=12,
            summary={
                "openLines": 12,
                "openQty": 5000.0,
                "overdueLines": 7,
                "excludedRows": 0,
            },
            bySupplier=[],
            lineRows=[],
        )
        assert "采购订单与到货记录" in result.answer


class TestDataLayerFact:
    def test_delivery_tracking_data_layer_is_dwd(self) -> None:
        """角标数据层与实际物理表一致（DWD_* 表 ⇒ DWD）。

        原值 `thbi_ods_erp` 来自设计期「ODS_ERP 是原始表」的对照；本体迁移后
        两个采购工具读的是同一批 DWD 宽表 ⇒ 角标继续写 ODS_ERP 属事实错误。
        """
        assert DATA_LAYER_LABEL == "thbi_dwd"
