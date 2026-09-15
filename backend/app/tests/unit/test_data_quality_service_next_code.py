"""DataQualityRuleService.nextRuleCode 单元测试（feat-rule-batch-create，2026-09-15）。

覆盖：
- 库内空 → 返回 seq=1
- 库内已存在 00001..00003 → 返回 seq=4
- 跨日（不同 date_yyyymmdd）→ 重新从 1 计数
- 类名清洗：非 ASCII / 特殊字符 → 'CLASS'；超长截断到 12
- 同类其它编码（前缀相近）不污染计数

session 用最小 fake，只实现 service 真实调用的 execute/scalar_one_or_none 链路。
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.enums import RuleType
from app.domain.models import DataQualityRule
from app.services.data_quality_service import DataQualityRuleService


def _makeFakeSession(existing_codes: list[str]) -> SimpleNamespace:
    """构造最小 session fake。

    service 只调用了 session.execute(stmt).scalar_one_or_none()，
    这里把 execute() 返回的 result 替换为 SimpleNamespace(scalar_one_or_none=...)。
    """

    def fake_execute(stmt: Any) -> SimpleNamespace:
        # 解析 stmt.where(...).limit(1) 的 where 条件里 like 后面的字面量。
        # 我们只关心 LIKE 前缀；ORM 不便直接取字面量，用 try/except 提取。
        prefix = _extractLikePrefix(stmt)
        if prefix is None:
            return SimpleNamespace(scalar_one_or_none=lambda: None)
        # 找库内匹配前缀的编码，返回 DESC 排序后的第一个（模拟 .order_by(desc).limit(1)）
        matched = sorted(
            [c for c in existing_codes if c.startswith(prefix)],
            reverse=True,
        )
        return SimpleNamespace(
            scalar_one_or_none=lambda: matched[0] if matched else None
        )

    return SimpleNamespace(execute=fake_execute)


def _extractLikePrefix(stmt: Any) -> str | None:
    """从 ORM stmt 里尽力提取 LIKE 前缀；ORM 内部结构不稳，失败兜 None。

    DataQualityRule.rule_code.like(f"{prefix}%") 在 SQLAlchemy 2.x 里
    通过 _whereclause 暴露，遍历 BinaryExpression 找 Like 节点。
    """
    try:
        # SQLAlchemy 2.x: stmt.whereclauses 是 list[ColumnElement]
        where = getattr(stmt, "whereclauses", None)
        if not where:
            return None
        for expr in where:
            op = getattr(expr, "operator", None)
            if op is not None and "like" in op.__name__.lower():
                right = expr.right
                # right 是 ParameterizedTyped 通常有 .value/effective_value
                val = getattr(right, "value", None) or getattr(
                    right, "effective_value", None
                )
                if isinstance(val, str) and val.endswith("%"):
                    return val[:-1]
        return None
    except Exception:
        return None


def _makeRule(code: str) -> DataQualityRule:
    """构造内存 ORM 实例（不写库），只填 nextRuleCode 用得到的字段。"""
    rule = DataQualityRule.__new__(DataQualityRule)
    rule.rule_code = code
    rule.rule_name = "test"
    rule.target_table = "T"
    rule.target_column = "C"
    rule.rule_type = RuleType.VALIDITY
    rule.datasource_id = 1
    rule.threshold = Decimal("95.00")
    rule.severity = "MEDIUM"
    rule.is_enabled = True
    rule.version = "v1.0"
    rule.derivation_type = "MANUAL"
    return rule


@pytest.mark.asyncio
async def test_nextRuleCode_empty_returns_1():
    session = _makeFakeSession([])
    svc = DataQualityRuleService()
    r = await svc.nextRuleCode(
        session, class_name="PURCHASE_ORDER", date_yyyymmdd="20260915"
    )
    assert r["code"] == "MU-DQ-PURCHASE_ORDER-20260915-00001"
    assert r["seq"] == 1


@pytest.mark.asyncio
async def test_nextRuleCode_existing_returns_max_plus_one():
    existing = [
        "MU-DQ-PURCHASE_ORDER-20260915-00001",
        "MU-DQ-PURCHASE_ORDER-20260915-00002",
        "MU-DQ-PURCHASE_ORDER-20260915-00003",
    ]
    session = _makeFakeSession(existing)
    svc = DataQualityRuleService()
    r = await svc.nextRuleCode(
        session, class_name="PURCHASE_ORDER", date_yyyymmdd="20260915"
    )
    assert r["code"] == "MU-DQ-PURCHASE_ORDER-20260915-00004"
    assert r["seq"] == 4


@pytest.mark.asyncio
async def test_nextRuleCode_cross_day_resets():
    """同类的另一日 → 重新计数。"""
    existing = [
        "MU-DQ-PURCHASE_ORDER-20260915-00001",
        "MU-DQ-PURCHASE_ORDER-20260915-00002",
    ]
    session = _makeFakeSession(existing)
    svc = DataQualityRuleService()
    r = await svc.nextRuleCode(
        session, class_name="PURCHASE_ORDER", date_yyyymmdd="20260916"
    )
    assert r["code"] == "MU-DQ-PURCHASE_ORDER-20260916-00001"
    assert r["seq"] == 1


@pytest.mark.asyncio
async def test_nextRuleCode_other_class_does_not_pollute():
    """其它类的编码不应污染当前类的计数。"""
    existing = [
        "MU-DQ-SUPPLIER-20260915-00007",
        "MU-DQ-SUPPLIER-20260915-00008",
    ]
    session = _makeFakeSession(existing)
    svc = DataQualityRuleService()
    r = await svc.nextRuleCode(
        session, class_name="PURCHASE_ORDER", date_yyyymmdd="20260915"
    )
    assert r["code"] == "MU-DQ-PURCHASE_ORDER-20260915-00001"
    assert r["seq"] == 1


@pytest.mark.asyncio
async def test_nextRuleCode_sanitizes_class_name():
    """非 ASCII / 特殊字符清洗 + 超长截断。"""
    session = _makeFakeSession([])
    svc = DataQualityRuleService()
    # 非 ASCII 字符（中文）→ 全空 → CLASS
    r = await svc.nextRuleCode(
        session, class_name="采购订单", date_yyyymmdd="20260915"
    )
    assert r["code"] == "MU-DQ-CLASS-20260915-00001"
    assert r["class_name"] == "CLASS"

    # 混合 ASCII + 空白/标点 → PURCHASE_ORDER
    r2 = await svc.nextRuleCode(
        session, class_name="Purchase Order", date_yyyymmdd="20260915"
    )
    assert r2["class_name"] == "PURCHASE_ORDER"

    # 超长截断到 12 字符
    r3 = await svc.nextRuleCode(
        session,
        class_name="VERY_LONG_CLASS_NAME_THAT_EXCEEDS",
        date_yyyymmdd="20260915",
    )
    assert r3["class_name"] == "VERY_LONG_CL"