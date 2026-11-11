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
