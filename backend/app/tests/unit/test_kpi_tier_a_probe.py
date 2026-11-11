"""Tier A 探针（scripts/kpi_tier_a_probe.py）的目标库护栏。

重点：``create_async_engine`` 会把 URL query（``?host=`` / ``?database=`` /
``?port=``）合并进连接参数，若只校验 authority/path，就能在库名合法时把连接
导向别的主机/库。故探针**拒绝任何非空 query**，且拒绝信息只打印 key 列表。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import kpi_tier_a_probe as probe

_BACKEND = Path(__file__).resolve().parents[3]
_SCRIPT = _BACKEND / "scripts" / "kpi_tier_a_probe.py"

_BASE = "postgresql+asyncpg://qa_user:secret-looking@"


def _assertRejected(databaseUrl: str) -> None:
    with pytest.raises(SystemExit) as excInfo:
        probe.assertAllowedTarget(databaseUrl)
    assert excInfo.value.code == 2


def test_allows_test_db_on_any_host() -> None:
    assert probe.assertAllowedTarget(_BASE + "10.0.0.9:5434/qa_metadata_test") == (
        "qa_metadata_test",
        "10.0.0.9",
    )
    assert probe.assertAllowedTarget(_BASE + "10.0.0.9:5434/qa_metadata_test_solo") == (
        "qa_metadata_test_solo",
        "10.0.0.9",
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", "127.0.0.1"), ("localhost", "localhost"), ("[::1]", "::1")],
)
def test_allows_dev_db_only_on_loopback(host: str, expected: str) -> None:
    assert probe.assertAllowedTarget(
        _BASE + f"{host}:5433/qa_metadata"
    ) == ("qa_metadata", expected)


def test_rejects_dev_db_name_on_remote_host() -> None:
    _assertRejected(_BASE + "192.168.1.10:5433/qa_metadata")


def test_rejects_unrelated_db_names() -> None:
    _assertRejected(_BASE + "127.0.0.1:5433/qa_metadata_prod")
    _assertRejected(_BASE + "127.0.0.1:5433/postgres")


def test_rejects_query_host_and_never_prints_its_value(capsys) -> None:
    _assertRejected(
        _BASE + "127.0.0.1:5433/qa_metadata?host=10.0.0.9"
    )
    stderr = capsys.readouterr().err
    assert "keys=['host']" in stderr
    assert "10.0.0.9" not in stderr
    assert "secret-looking" not in stderr


def test_rejects_query_database_and_never_prints_its_value(capsys) -> None:
    _assertRejected(
        _BASE + "10.0.0.9:5434/qa_metadata_test?database=qa_metadata"
    )
    stderr = capsys.readouterr().err
    assert "keys=['database']" in stderr
    # 只打印 key；值是非法目标也不回显（值可能携带凭据类信息）
    assert "qa_metadata" not in stderr.split("keys=")[-1]


def test_rejects_unparseable_url() -> None:
    _assertRejected("not-a-url")


def test_direct_script_help_works() -> None:
    """Minor 1：`python scripts/kpi_tier_a_probe.py --help` 不得 ModuleNotFoundError。"""
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        cwd=_BACKEND,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--database-url" in completed.stdout
    assert "--confirm-target" in completed.stdout


# ===========================================================================
# --execute 业务库目标确认（安全终审 M4/M5）
# ===========================================================================

_FAKE_DATABASE_URL = "postgresql+asyncpg://qa_user:secret@127.0.0.1:5433/qa_metadata"


def _fakeKpi():
    from app.domain.models import KpiCatalog

    return KpiCatalog(
        kpi_code="KPI_PROBE",
        kpi_name="探针指标",
        metric_id=1,
        datasource_id=1,
        numerator_columns=["qty"],
        grain_keys=["supplier_id"],
        status="PUBLISHED",
    )


def _fakeMetric():
    from app.domain.models import OntologyMetric

    return OntologyMetric(
        metric_name="KPI_PROBE-metric",
        formula="SELECT SUM(qty) FROM delivery GROUP BY supplier_id",
        metric_type="MEASURE",
        numerator_measure={"agg": "SUM", "expr": "qty"},
        grain_keys=["supplier_id"],
        execution_source="ESCAPE_HATCH",
    )


def _fakeDatasource():
    from app.domain.enums import DataSourceType
    from app.domain.models import DataSource

    return DataSource(
        id=1,
        name="thbi",
        type=DataSourceType.ORACLE,
        host="10.0.0.5",
        port=1521,
        database_name="SVC",
        username="secret-user",
        password_encrypted="secret-cipher",
    )


class _Scalars:
    def __init__(self, items) -> None:
        self._items = items

    def all(self):
        return list(self._items)


class _Result:
    def __init__(self, items) -> None:
        self._items = items

    def scalars(self) -> _Scalars:
        return _Scalars(self._items)


class _FakeSession:
    def __init__(self) -> None:
        self._kpi = _fakeKpi()
        self._metric = _fakeMetric()
        self._datasource = _fakeDatasource()

    async def execute(self, _stmt):
        return _Result([self._kpi])

    async def get(self, model, _pk):
        from app.domain.models import OntologyMetric

        return self._metric if model is OntologyMetric else self._datasource


def test_parse_args_confirm_target_defaults_false() -> None:
    args = probe._parseArgs(
        ["--database-url", _FAKE_DATABASE_URL, "--execute",
         "--kpi-code", "KPI_PROBE", "--datasource-id", "1"]
    )
    assert args.confirm_target is False


def test_parse_args_accepts_confirm_target() -> None:
    args = probe._parseArgs(
        ["--database-url", _FAKE_DATABASE_URL, "--execute",
         "--kpi-code", "KPI_PROBE", "--datasource-id", "1", "--confirm-target"]
    )
    assert args.confirm_target is True


@pytest.mark.asyncio()
async def test_execute_without_confirm_prints_target_and_skips_query(
    capsys, monkeypatch
) -> None:
    """缺 --confirm-target：只打印目标（不含用户名/口令），绝不执行查询，退出 3。"""
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("未确认目标时不得构造执行服务")

    monkeypatch.setattr(probe, "KpiExecutionService", _forbidden)
    code = await probe.runExecute(_FakeSession(), "KPI_PROBE", 1, confirmTarget=False)
    captured = capsys.readouterr()

    assert code == 3
    assert "type=ORACLE" in captured.out
    assert "host=10.0.0.5" in captured.out
    assert "port=1521" in captured.out
    assert "database=SVC" in captured.out
    assert "secret-user" not in captured.out + captured.err
    assert "secret-cipher" not in captured.out + captured.err
    assert "--confirm-target" in captured.err


def test_main_masks_exception_detail(monkeypatch, capsys) -> None:
    """安全终审 M5：异常兜底只打印类名 + 详情已省略（原文可能含 host/user）。"""
    async def _boom(_args):
        raise RuntimeError("host=10.0.0.5 user=secret-user")

    monkeypatch.setattr(probe, "_run", _boom)
    code = probe.main(["--database-url", _FAKE_DATABASE_URL])
    stderr = capsys.readouterr().err

    assert code == 1
    assert "RuntimeError" in stderr
    assert "详情已省略" in stderr
    assert "10.0.0.5" not in stderr
    assert "secret-user" not in stderr
