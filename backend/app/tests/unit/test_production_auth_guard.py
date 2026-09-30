"""生产鉴权配置自检守卫（安全批次 · 路由鉴权收口，修复轮 1 / I-2）。

背景：`app/tests/integration/test_route_auth_guard.py` 是**结构性**守卫，断言
「非白名单路由的依赖链里有 `Depends(getCurrentUser)`」。但 `getCurrentUser` 在
`AUTH_MODE=stub` 下把无头请求解析为 `anonymous`（roles 含 user/admin）而**不抛异常**，
于是：

    APP_ENV=production AUTH_MODE=stub（或漏设 AUTH_MODE，默认就是 stub）
    ⇒ 46 条非公开路由依然匿名可达，而路由守卫测试**全绿**。

`main.py` 启动自检当时只检查 `AUTH_STUB_ENABLED`，**漏了 `AUTH_MODE`**，且只
`logger.error`（fail-soft，不阻塞启动）⇒ 容器照常起、CI 照常绿。

本守卫把「生产跑 stub」这一条件变成**可被测试检出**的纯函数
`productionAuthMisconfiguration`，并断言 `main.py` 真的接线了它。

纯函数、无 DB：本文件覆写 conftest 的三个拉 PG 的 fixture（同
`test_kpi_semantic_match_service.py` 的先例），不付真库代价。
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from app import __file__ as appInitFile
from app.config import Settings, productionAuthMisconfiguration

_APP_DIR = Path(appInitFile).resolve().parent
_MAIN_PY = _APP_DIR / "main.py"


# ---------------------------------------------------------------------------
# 覆写 conftest 的 autouse fixture：纯函数测试不需要 DB。
# ---------------------------------------------------------------------------

@pytest.fixture()
def dbSession() -> Any:
    """覆写 conftest autouse dbSession —— 本文件不用 DB。"""
    return None


@pytest_asyncio.fixture()
async def seedEngine() -> Any:
    """覆写 conftest autouse seedEngine —— 本文件不用 DB。"""
    return None


@pytest_asyncio.fixture()
async def warmBusinessObjectRegistry() -> Any:
    """覆写 conftest autouse warmBusinessObjectRegistry —— 无需 DB 的 no-op。"""
    yield


# ---------------------------------------------------------------------------
# 纯函数分支
# ---------------------------------------------------------------------------

def test_production_with_stub_mode_is_flagged() -> None:
    """生产 + AUTH_MODE=stub ⇒ 必须报错，且原因指向 AUTH_MODE。"""
    reason = productionAuthMisconfiguration(
        Settings(APP_ENV="production", AUTH_MODE="stub", AUTH_STUB_ENABLED=False)
    )
    assert reason is not None
    assert "AUTH_MODE" in reason


def test_production_with_default_auth_mode_is_flagged() -> None:
    """本发现的核心场景：生产**漏设** AUTH_MODE ⇒ 取默认值 stub，必须被逮到。

    这条是「容器正常起、守卫绿、CI 绿」那条路径的最小复现。
    """
    settings = Settings(APP_ENV="production")
    assert settings.authMode == "stub", "默认值变了——本守卫的前提需重新评估"
    assert productionAuthMisconfiguration(settings) is not None


def test_production_with_real_mode_passes() -> None:
    """生产 + real + 关掉 stub 头 ⇒ 配置正确，返回 None。"""
    assert productionAuthMisconfiguration(
        Settings(APP_ENV="production", AUTH_MODE="real", AUTH_STUB_ENABLED=False)
    ) is None


def test_development_with_stub_mode_passes() -> None:
    """非生产环境跑 stub 是设计内的 dev 体验，不得报错。"""
    assert productionAuthMisconfiguration(
        Settings(APP_ENV="development", AUTH_MODE="stub", AUTH_STUB_ENABLED=True)
    ) is None


def test_production_real_mode_with_stub_headers_is_flagged() -> None:
    """第二个独立情形：AUTH_MODE=real 但 AUTH_STUB_ENABLED=1 仍可伪造 X-User-* 头。"""
    reason = productionAuthMisconfiguration(
        Settings(APP_ENV="production", AUTH_MODE="real", AUTH_STUB_ENABLED=True)
    )
    assert reason is not None
    assert "AUTH_STUB_ENABLED" in reason


# ---------------------------------------------------------------------------
# 装配守卫：自检函数不能在 config.py 里休眠
# ---------------------------------------------------------------------------

def test_main_lifespan_wires_the_guard() -> None:
    """main.py 骨架（剥掉 STRING/COMMENT，防 docstring 误命中）必须引用自检函数。"""
    codeOnly = io.StringIO()
    with tokenize.open(str(_MAIN_PY)) as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            codeOnly.write(tok.string)
    assert "productionAuthMisconfiguration" in codeOnly.getvalue(), (
        "main.py 未接线 productionAuthMisconfiguration 启动自检（守卫在 config.py 里休眠）"
    )
