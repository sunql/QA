"""业务库 adapter 单次 maxRows 上限：_effectiveRowLimit 归一化语义。

显式 maxRows 与 QUERY_ROW_LIMIT 取更小正数；两者都为 0/None ⇒ 不限行。
"""

from __future__ import annotations

from app.infrastructure.business_db_pool import _effectiveRowLimit


def test_explicit_max_rows_wins(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.infrastructure.business_db_pool.getSettings",
        lambda: type("S", (), {"queryRowLimit": 0})(),
    )
    assert _effectiveRowLimit(1000) == 1000


def test_configured_limit_is_upper_bound(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.infrastructure.business_db_pool.getSettings",
        lambda: type("S", (), {"queryRowLimit": 50})(),
    )
    assert _effectiveRowLimit(1000) == 50
    assert _effectiveRowLimit(10) == 10


def test_no_max_rows_keeps_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.infrastructure.business_db_pool.getSettings",
        lambda: type("S", (), {"queryRowLimit": 0})(),
    )
    assert _effectiveRowLimit(None) == 0
