"""场景提问渲染单测（纯函数）：冲突让位 + 模板替换 + 必填槽位校验。"""

from __future__ import annotations

import pytest

from app.domain.exceptions import ValidationError
from app.services.copilot_scenario_render import renderScenarioQuestion

SCHEMA = [
    {"key": "monthsBack", "type": "number", "label": "时间窗",
     "required": True, "conflictKeywords": ["时间窗", "个月"]},
    {"key": "supplierKeys", "type": "entityMulti", "label": "供应商",
     "conflictKeywords": ["供应商", "厂家"]},
    {"key": "materialKeys", "type": "entityMulti", "label": "物料",
     "conflictKeywords": ["物料", "材料"]},
    {"key": "siteKeys", "type": "dictMulti", "label": "基地",
     "conflictKeywords": ["基地", "厂区", "工厂"]},
    {"key": "minOverdueDays", "type": "number", "label": "最低逾期天数"},
]

SLOTS = {
    "monthsBack": 12,
    "supplierKeys": ["B125"],
    "minOverdueDays": 0,
}


TEMPLATE = "分析待到货情况"


class TestRenderScenarioQuestion:
    def test_full_tail_when_no_message(self) -> None:
        out = renderScenarioQuestion(
            template=TEMPLATE, slots=SLOTS, slotSchema=SCHEMA, message=None,
        )
        assert out.startswith("分析待到货情况（")
        assert "供应商=B125" in out
        assert "supplierKeys=[B125]" in out
        # number 槽位渲染原始值（displayFormat 留给场景配置层，保持渲染器通用）
        assert "时间窗=12" in out
        assert "以用户问题描述为准" in out

    def test_code_mention_yields_supplier_slot(self) -> None:
        """纯编码提问（无「供应商」关键字）也算指定——生产实证漏判回归。"""
        out = renderScenarioQuestion(
            template=TEMPLATE, slots=SLOTS, slotSchema=SCHEMA,
            message="分析一下B109待到货情况",
        )
        assert "B109" in out
        # 编码命中 ⇒ supplier 让位：tail 与 tool args 都不携带
        assert "供应商=" not in out
        assert "supplierKeys=" not in out
        # 非冲突维度保留
        assert "时间窗=12" in out
        assert "最低逾期天数=0" in out

    def test_keyword_mention_yields_supplier_slot(self) -> None:
        out = renderScenarioQuestion(
            template=TEMPLATE, slots=SLOTS, slotSchema=SCHEMA,
            message="分析B019这个供应商的待到货情况",
        )
        assert "supplierKeys=[B125]" not in out

    def test_list_values_joined_with_comma_in_args(self) -> None:
        out = renderScenarioQuestion(
            template=TEMPLATE,
            slots={**SLOTS, "supplierKeys": ["F109", "B125"]},
            slotSchema=SCHEMA, message=None,
        )
        assert "supplierKeys=[F109, B125]" in out
        assert "[；" not in out  # 旧格式毛刺回归

    def test_missing_required_slot_raises_when_no_message(self) -> None:
        schema = [
            {"key": "materialKeys", "type": "entityMulti", "label": "物料",
             "required": True},
        ]
        with pytest.raises(ValidationError, match="缺少场景必填槽位"):
            renderScenarioQuestion(
                template=TEMPLATE, slots={}, slotSchema=schema, message=None,
            )

    def test_template_placeholder_substituted_when_no_message(self) -> None:
        """无补充文字时模板作为主句，``{slot}`` 占位用槽位值替换。"""
        out = renderScenarioQuestion(
            template="分析待到货情况（近{monthsBack}个月）",
            slots=SLOTS, slotSchema=SCHEMA, message=None,
        )
        assert out.startswith("分析待到货情况（近12个月）（")

    def test_message_supplies_missing_slot(self) -> None:
        """有文字时缺槽位不报错——文字即取数意图。"""
        schema = [
            {"key": "materialKeys", "type": "entityMulti", "label": "物料",
             "required": True},
        ]
        out = renderScenarioQuestion(
            template=TEMPLATE, slots={}, slotSchema=schema,
            message="分析502447700001的待到货情况",
        )
        assert "502447700001" in out
