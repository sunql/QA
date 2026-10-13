"""P1-5 Hypothesis.verify() 单元测试（RED）。

覆盖：
- supported：SQL 返回匹配数据的行（support_count >= 1）
- contradicted：SQL 返回 `opposite` 标记行为真
- inconclusive：SQL 返回空行
- execution_failed：SQL runner 抛异常
"""

from __future__ import annotations

import pytest
from app.services.hypothesis_service import Hypothesis, VerificationResult


class _FakeRunner:
    """伪造 sql_runner：只读 SELECT，SQL Guard 在调用方生效。"""

    def __init__(
        self,
        returning: list[dict] | None = None,
        raises: Exception | None = None,
    ):
        self.returning = returning or []
        self.raises = raises
        self.calls: list[str] = []

    async def __call__(self, sql: str) -> list[dict]:
        self.calls.append(sql)
        if self.raises:
            raise self.raises
        return self.returning


@pytest.mark.asyncio
async def test_zero_support_count_returns_inconclusive():
    """无数据行 → inconclusive（不是 supported）。"""
    h = Hypothesis(statement="采购金额上升", driver="AMT", verificationSql="SELECT 1")
    runner = _FakeRunner(returning=[])
    result = await h.verify(sql_runner=runner)
    assert result.verdict == "inconclusive"
    assert result.support_count == 0
    assert result.execution_succeeded is True


@pytest.mark.asyncio
async def test_contradicted_when_data_opposite():
    """返回 `opposite=True` 行 → contradicted（假设方向与数据相反）。"""
    h = Hypothesis(statement="采购金额下降", driver="AMT", verificationSql="SELECT 1")
    runner = _FakeRunner(returning=[{"opposite": True}])
    result = await h.verify(sql_runner=runner)
    assert result.verdict == "contradicted"
    assert result.execution_succeeded is True


@pytest.mark.asyncio
async def test_execution_failed_separated():
    """sql_runner 抛异常 → execution_failed（不是 contradicted）。"""
    h = Hypothesis(statement="...", driver=None, verificationSql="INVALID SQL")
    runner = _FakeRunner(raises=ValueError("parse error"))
    result = await h.verify(sql_runner=runner)
    assert result.verdict == "execution_failed"
    assert result.execution_succeeded is False


@pytest.mark.asyncio
async def test_supported_when_rows_match_pattern():
    """返回数据行（无 opposite 标记）→ supported，support_count 反映实际行数。"""
    h = Hypothesis(statement="采购金额上升", driver="AMT", verificationSql="SELECT 1")
    runner = _FakeRunner(returning=[{"AMT_2024": 100}, {"AMT_2024": 200}])
    result = await h.verify(sql_runner=runner)
    assert result.verdict == "supported"
    assert result.support_count == 2
    assert result.execution_succeeded is True


@pytest.mark.asyncio
async def test_verification_result_fields_complete():
    """VerificationResult 包含 rows_sample（最多 3 行）。"""
    h = Hypothesis(statement="测试", driver="X", verificationSql="SELECT 1")
    runner = _FakeRunner(
        returning=[
            {"a": 1},
            {"a": 2},
            {"a": 3},
            {"a": 4},  # 超过 3 行上限
        ]
    )
    result = await h.verify(sql_runner=runner)
    assert result.verdict == "supported"
    assert len(result.rows_sample) == 3  # 最多 3 行
    assert result.rows_sample[0] == {"a": 1}
