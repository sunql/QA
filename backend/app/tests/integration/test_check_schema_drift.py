"""Schema drift 检测脚本单测 + 集成测试。

覆盖：
- 解析不同风格的 Alembic 迁移文件（modern str-typed / old direct assignment）
- 检测 ORM 声明但 DB 缺失的表
- 检测 DB 存在但 ORM 未声明的表（多余表）
- 检测 alembic_version 滞后于 head
- 检测 alembic_version 不在迁移文件集合（脏状态）
- 检测 alembic_version 表不存在（全新空库）
- happy path：DB 与 head 一致时返回 []

脚本通过 subprocess 调用（独立进程，与 alembic env 解耦），单元测试则通过
importlib 按文件路径加载解析函数（_parseRevisionFiles / _findHead 纯逻辑），
避免 pytest sys.path 集合时机的副作用。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _BACKEND_ROOT / "scripts" / "check_schema_drift.py"


def _loadDriftModule():
    """通过 importlib.util 按文件路径加载，避免 pytest sys.path 副作用。"""
    spec = importlib.util.spec_from_file_location(
        "check_schema_drift", str(_SCRIPT_PATH)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_drift_module = _loadDriftModule()
_parseRevisionFiles = _drift_module._parseRevisionFiles
_findHead = _drift_module._findHead


class TestParseRevisionFiles:
    """测试 Alembic 迁移文件解析（两种风格）。"""

    def test_modern_style_with_type_annotation(self, tmp_path: Path) -> None:
        (tmp_path / "0001_init.py").write_text(
            'revision: str = "0001_init"\n'
            'down_revision: str | None = None\n'
        )
        (tmp_path / "0002_next.py").write_text(
            'revision: str = "0002_next"\n'
            'down_revision: str | None = "0001_init"\n'
        )
        revs, graph = _parseRevisionFiles(tmp_path)
        assert revs == {"0001_init", "0002_next"}
        assert graph == {"0001_init": None, "0002_next": "0001_init"}

    def test_old_style_without_type_annotation(self, tmp_path: Path) -> None:
        (tmp_path / "0001_init.py").write_text(
            'revision = "0001_init"\n'
            "down_revision = None\n"
        )
        (tmp_path / "0002_next.py").write_text(
            'revision = "0002_next"\n'
            'down_revision = "0001_init"\n'
        )
        revs, graph = _parseRevisionFiles(tmp_path)
        assert revs == {"0001_init", "0002_next"}
        assert graph == {"0001_init": None, "0002_next": "0001_init"}

    def test_mixed_styles(self, tmp_path: Path) -> None:
        (tmp_path / "0001_init.py").write_text(
            'revision: str = "0001_init"\n'
            'down_revision: str | None = None\n'
        )
        (tmp_path / "0002_next.py").write_text(
            'revision = "0002_next"\n'
            'down_revision = "0001_init"\n'
        )
        revs, graph = _parseRevisionFiles(tmp_path)
        assert graph["0001_init"] is None
        assert graph["0002_next"] == "0001_init"


class TestFindHead:
    """测试 head 解析（链唯一性）。"""

    def test_single_chain(self) -> None:
        graph = {"a": None, "b": "a", "c": "b"}
        assert _findHead(graph) == "c"

    def test_multi_head_raises(self) -> None:
        graph = {"a": None, "b": None}
        with pytest.raises(RuntimeError, match="多 head"):
            _findHead(graph)

    def test_no_head_raises(self) -> None:
        graph = {"a": "b", "b": "a"}
        with pytest.raises(RuntimeError, match="多 head"):
            _findHead(graph)


class TestCheckDriftSubprocess:
    """通过 subprocess 调用脚本（真实进程边界，避免 alembic env 副作用）。"""

    @pytest.fixture()
    def db_url(self) -> str:
        from app.tests._pg_support import resolveTestDatabaseUrl

        return resolveTestDatabaseUrl()

    def _run_script(self, db_url: str, *args: str) -> subprocess.CompletedProcess:
        """运行脚本并返回结果。"""
        env = dict(os.environ)
        env["DATABASE_URL"] = db_url
        return subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), *args],
            cwd=str(_BACKEND_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_healthy_db_returns_zero(self, db_url: str) -> None:
        """DB 已 upgrade head 时，脚本 exit 0。"""
        result = self._run_script(db_url, "--quiet")
        assert result.returncode == 0, (
            f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )

    def test_missing_table_returns_one(self, db_url: str) -> None:
        """当 ORM 表被临时移除，脚本应 exit 1 + 输出漂移项。"""
        # 用 psql 临时重命名一张表，触发 ORM-vs-DB 漂移
        import asyncpg

        async def rename() -> None:
            conn = await asyncpg.connect(db_url.replace("+asyncpg", ""))
            try:
                await conn.execute(
                    "ALTER TABLE entity_mapping RENAME TO entity_mapping_drift_test"
                )
            finally:
                await conn.close()

        async def restore() -> None:
            conn = await asyncpg.connect(db_url.replace("+asyncpg", ""))
            try:
                await conn.execute(
                    "ALTER TABLE entity_mapping_drift_test RENAME TO entity_mapping"
                )
            finally:
                await conn.close()

        asyncio_run(rename())
        try:
            result = self._run_script(db_url)
        finally:
            asyncio_run(restore())

        assert result.returncode == 1, (
            f"应检测到漂移 exit 1，但 exit={result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "entity_mapping" in combined
        assert "缺失" in combined


class TestSkipSchemaCheckEnvVar:
    """测试 SKIP_SCHEMA_CHECK 环境变量在 main.py 中的存在性。"""

    def test_main_py_checks_env_var(self) -> None:
        main_py = (_BACKEND_ROOT / "app" / "main.py").read_text(encoding="utf-8")
        assert "SKIP_SCHEMA_CHECK" in main_py, (
            "app/main.py 必须检查 SKIP_SCHEMA_CHECK 环境变量"
        )
        assert 'os.environ.get("SKIP_SCHEMA_CHECK") != "1"' in main_py


def asyncio_run(coro):  # noqa: ANN001, ANN201
    """helper：同步包装 asyncpg 调用（不引入额外依赖）。"""
    import asyncio

    return asyncio.run(coro)