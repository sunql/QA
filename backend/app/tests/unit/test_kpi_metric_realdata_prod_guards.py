"""F7/F8：realdata 脚本在正式库上的写入边界。

- S4 需要临时改写 ``system_config`` 灰度开关 ⇒ prod **一律 SKIP，不读不写**；
- S9 回填写库 ⇒ prod **必须先备份再写**，备份失败不得进入写入。
"""

from __future__ import annotations

import importlib
import sys

import pytest

_TEST_DSN = (
    "postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5434/"
    "qa_metadata_test_solo"
)


def _loadRealdataModule(monkeypatch: pytest.MonkeyPatch):
    """入口模块在 import 期解析 DSN ⇒ 必须先设专用环境变量。

    已 import 过则直接复用（模块级 ``_IS_PROD`` 由首次 DSN 决定；本文件所有
    用例都显式传 ``isProd``，不依赖它）。
    """
    monkeypatch.setenv("KPI_METRIC_REALDATA_DATABASE_URL", _TEST_DSN)
    return importlib.import_module("scripts.kpi_metric_realdata")


# ── F8：prod 的 S4 不读不写 ──────────────────────────────────────────


async def test_step4_prod_skips_without_any_switch_read_or_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import kpi_metric_realdata_s4 as s4Module
    from scripts.kpi_metric_realdata_lib import SKIPS

    calls: list[str] = []

    async def _write(*_args: object, **_kwargs: object) -> None:
        calls.append("write")

    async def _read(*_args: object, **_kwargs: object) -> str | None:
        calls.append("read")
        return None

    async def _fetch(*_args: object, **_kwargs: object) -> list[object]:
        calls.append("fetch")
        return []

    monkeypatch.setattr(s4Module, "writeSwitchValue", _write)
    monkeypatch.setattr(s4Module, "readSwitchValue", _read)
    monkeypatch.setattr(s4Module, "fetchKpis", _fetch)
    SKIPS.clear()
    try:
        await s4Module.step4_publishGate(object(), isProd=True)  # type: ignore[arg-type]
        assert calls == []
        assert any("S4" in name for name, _reason in SKIPS)
    finally:
        SKIPS.clear()


# ── F7：prod 的 S9 先备份再写，备份失败即停 ──────────────────────────


class _FakeApplySummary:
    def __init__(self) -> None:
        self.created: tuple[str, ...] = ()
        self.updated: tuple[str, ...] = ()
        self.degraded: tuple[str, ...] = ()
        self.skipped: tuple[str, ...] = ()
        self.unbound_data_source: tuple[str, ...] = ()
        self.unresolved_classes: tuple[str, ...] = ()
        self.reverted_to_draft: tuple[str, ...] = ()


def _patchStep9Deps(module, monkeypatch: pytest.MonkeyPatch, events: list[str]):
    monkeypatch.setattr(module, "buildPlans", lambda: [])

    async def _count(_session: object) -> int:
        return 0

    async def _fetch(_session: object) -> list[object]:
        return []

    async def _apply(*_args: object, **_kwargs: object) -> _FakeApplySummary:
        events.append("apply")
        return _FakeApplySummary()

    async def _revision(_session: object) -> str:
        return "0137"

    monkeypatch.setattr(module, "countMetrics", _count)
    monkeypatch.setattr(module, "fetchKpis", _fetch)
    monkeypatch.setattr(module, "applyPlans", _apply)
    monkeypatch.setattr(module, "readAlembicVersion", _revision)
    module._ALEMBIC_VERSION = ""


async def test_step9_prod_backs_up_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loadRealdataModule(monkeypatch)
    events: list[str] = []
    _patchStep9Deps(module, monkeypatch, events)
    backupCalls: list[tuple[str, str]] = []

    def _backup(dsn: str, revision: str) -> tuple:
        events.append("backup")
        backupCalls.append((dsn, revision))
        return ()

    monkeypatch.setattr(module, "_ensureProdBackup", _backup)
    monkeypatch.setattr(module, "_printBanner", lambda *_a: events.append("banner"))

    await module.step9_applyBackfill(object(), isProd=True)

    assert backupCalls == [(module.DATABASE_URL, "0137")]
    assert events.index("backup") < events.index("apply")


async def test_step9_prod_backup_failure_stops_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _loadRealdataModule(monkeypatch)
    events: list[str] = []
    _patchStep9Deps(module, monkeypatch, events)

    def _backup(_dsn: str, _revision: str) -> tuple:
        raise SystemExit("备份失败：pg_dump 不可用")

    monkeypatch.setattr(module, "_ensureProdBackup", _backup)
    monkeypatch.setattr(module, "_printBanner", lambda *_a: events.append("banner"))

    with pytest.raises(SystemExit, match="备份失败"):
        await module.step9_applyBackfill(object(), isProd=True)

    assert "apply" not in events


async def test_step9_test_database_does_not_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试库不需要备份（保持现状）。"""
    module = _loadRealdataModule(monkeypatch)
    events: list[str] = []
    _patchStep9Deps(module, monkeypatch, events)

    def _backup(*_args: object, **_kwargs: object) -> tuple:
        raise AssertionError("测试库不应触发备份")

    monkeypatch.setattr(module, "_ensureProdBackup", _backup)

    await module.step9_applyBackfill(object(), isProd=False)

    # S9 固定跑两遍（幂等验证）⇒ 两次 apply，且全程没有 backup/banner。
    assert events == ["apply", "apply"]


def test_entry_module_imported_with_expected_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冒烟：入口模块可加载且 ``_IS_PROD`` 由 DSN 派生（不是写死字面量）。"""
    module = _loadRealdataModule(monkeypatch)
    assert module._IS_PROD is (module.TARGET_DB == module.PROD_DB_NAME)
    assert "scripts.kpi_metric_realdata" in sys.modules
