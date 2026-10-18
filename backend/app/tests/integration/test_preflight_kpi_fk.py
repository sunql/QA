"""preflight_kpi_fk.py 集成测试（真实 PG，head 状态）。

验证部署门禁脚本：
1. head 状态下三项检查全部 PASS（版本 >= 0132 + FK RESTRICT）
2. 反向守卫：约束名写错时必须 FAIL（证明真的查了 pg_constraint，不是常量假绿）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

_BACKEND_ROOT = Path(__file__).resolve().parents[3]  # backend/
_SCRIPT = _BACKEND_ROOT / "scripts" / "preflight_kpi_fk.py"


def _loadModule():
    spec = importlib.util.spec_from_file_location("preflight_kpi_fk", str(_SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 需要能通过 cls.__module__ 在 sys.modules 里找到模块
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_preflight_all_checks_pass_at_head(dbSession: AsyncSession) -> None:
    preflight = _loadModule()
    results = await preflight.runChecks(dbSession)

    assert [r.name for r in results] == [
        "alembic_version",
        "fk_on_delete",
        "bound_kpi_rows",
    ]
    assert all(r.passed for r in results), [r.detail for r in results if not r.passed]
    assert results[0].detail.startswith("current=0132") or ">=0132" in results[0].detail
    assert "'r'" in results[1].detail


@pytest.mark.asyncio
async def test_preflight_fk_check_fails_on_wrong_constraint_name(
    dbSession: AsyncSession,
) -> None:
    """反向守卫：换成不存在的约束名 → fk_on_delete 必须 FAIL。"""
    preflight = _loadModule()
    original = preflight._CONSTRAINT
    preflight._CONSTRAINT = "no_such_constraint"
    try:
        results = await preflight.runChecks(dbSession)
    finally:
        preflight._CONSTRAINT = original

    by_name = {r.name: r for r in results}
    assert by_name["fk_on_delete"].passed is False
    assert "None" in by_name["fk_on_delete"].detail
