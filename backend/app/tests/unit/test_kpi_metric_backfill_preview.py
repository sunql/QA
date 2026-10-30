"""预览阶段读目标库事实的降级行为（0135 prod 缺 `ontology_metric.status`）。

`_metricFacts` 只允许对「缺 status 列」这一种情况降级，其余缺列必须照抛。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import ProgrammingError

import scripts.kpi_metric_backfill as cliModule


class _RowsResult:
    def __init__(self, rows: list[tuple[str, str] | str]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, str] | str]:
        return self._rows

    def scalars(self) -> _RowsResult:
        return self


class _MetricFactsSession:
    """首次 `execute` 按需抛错，之后返回预置行（模拟 0135 prod 缺列）。"""

    def __init__(self, error: Exception | None, rows: list[tuple[str, str] | str]) -> None:
        self._error = error
        self._rows = rows
        self.calls = 0
        self.rollbacks = 0

    async def execute(self, _statement: object) -> _RowsResult:
        self.calls += 1
        if self._error is not None and self.calls == 1:
            raise self._error
        return _RowsResult(self._rows)

    async def rollback(self) -> None:
        self.rollbacks += 1


def _undefinedColumn(
    column: str, statement: str = "SELECT 1"
) -> ProgrammingError:
    """构造真实形态：SQL 文本里带表名限定列，orig 里是 PostgreSQL 缺列消息。"""
    orig = RuntimeError(f'column "{column}" does not exist')
    orig.sqlstate = "42703"  # type: ignore[attr-defined]
    return ProgrammingError(statement, {}, orig)


async def test_metric_facts_degrades_when_prod_lacks_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """0135 的 prod 没有 status ⇒ 退化为「只读名字、不判定 PUBLISHED」并明示。"""
    session = _MetricFactsSession(
        _undefinedColumn(
            "status",
            "SELECT ontology_metric.metric_name, ontology_metric.status FROM ontology_metric",
        ),
        ["KPI_A", "KPI_B"],
    )

    names, published = await cliModule._metricFacts(session)  # type: ignore[arg-type]

    assert names == frozenset({"KPI_A", "KPI_B"})
    assert published == frozenset()
    assert session.rollbacks == 1
    assert "不判定 PUBLISHED" in capsys.readouterr().out


async def test_metric_facts_propagates_other_missing_columns() -> None:
    """只豁免 ``ontology_metric.status`` 这一列。

    负向 SQL **故意含另一个表的 status 列**：这才能判出「裸 status」判据的
    误命中（旧实现只看 ``"status" in str(exc)`` ⇒ 会把 formula 缺列也当 status）。
    """
    session = _MetricFactsSession(
        _undefinedColumn(
            "formula",
            "SELECT kpi_catalog.status, ontology_metric.formula FROM ontology_metric",
        ),
        [],
    )
    with pytest.raises(ProgrammingError):
        await cliModule._metricFacts(session)  # type: ignore[arg-type]
