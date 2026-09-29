"""PlanResult / SqlResult dataclass cachedTokens 字段单元测试（4-1 回归）。

回归背景：feat-token-cache 初版只给 LlmResponse/StreamChunk/_SqlOutcome 加了
cachedTokens 字段，忘了 PlanResult / SqlResult —— 部署后 chat API 立刻爆
"'PlanResult' object has no attribute 'cachedTokens'"。本测试守住该契约。

TDD RED：未修改 dataclass 时 RED；添加 cachedTokens 字段后 GREEN。
"""

from __future__ import annotations

from dataclasses import fields

from app.domain.models import OntologyClass
from app.domain.query_plan import PlanResult, QueryPlan
from app.services.nl2sql_service import SqlResult


def _stub_plan() -> QueryPlan:
    """构造最小可序列化的 QueryPlan（不触 DB）。"""
    return QueryPlan(
        target="t",
    )


def test_planResult_has_cachedTokens_field() -> None:
    """PlanResult dataclass 必须声明 cachedTokens 字段（缺则 chat 端 AttributeError）。"""
    names = {f.name for f in fields(PlanResult)}
    assert "cachedTokens" in names


def test_sqlResult_has_cachedTokens_field() -> None:
    """SqlResult dataclass 必须声明 cachedTokens 字段。"""
    names = {f.name for f in fields(SqlResult)}
    assert "cachedTokens" in names


def test_planResult_cachedTokens_defaults_to_none() -> None:
    """默认 cachedTokens=None（OpenAI/MOONSHOT/AZURE 不支持时）。"""
    result = PlanResult(plan=_stub_plan(), promptTokens=100, completionTokens=50)
    assert result.cachedTokens is None


def test_sqlResult_cachedTokens_defaults_to_none() -> None:
    """默认 cachedTokens=None。"""
    result = SqlResult(sql="SELECT 1", promptTokens=100, completionTokens=50)
    assert result.cachedTokens is None


def test_planResult_cachedTokens_carries_deepseek_value() -> None:
    """DeepSeek 命中场景：cachedTokens 应能传入并保留。"""
    result = PlanResult(
        plan=_stub_plan(), promptTokens=1000, completionTokens=50,
        cachedTokens=800,
    )
    assert result.cachedTokens == 800


def test_sqlResult_cachedTokens_carries_deepseek_value() -> None:
    """SQL 阶段同上。"""
    result = SqlResult(sql="SELECT 1", promptTokens=500, completionTokens=50, cachedTokens=400)
    assert result.cachedTokens == 400