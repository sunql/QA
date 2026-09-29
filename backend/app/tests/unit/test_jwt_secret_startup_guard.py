"""jwtSecret 启动自检守卫（config 重复字段批）。

背景：`app/config.py` 曾有 9 组重复字段声明（后者静默覆盖前者），其中 `jwtSecret`
旧块声明 default=""（意图：漏配即暴露），但被新块的开发占位符
``"development-jwt-secret-change-me"`` 覆盖——结果**漏配 JWT_SECRET 时静默用公开已知的
密钥启动**，且旧块注释里写的「启动期校验」从未实现。本批删除重复声明的同时补上这个
休眠承诺：启动期对空值/占位符大声 warning（不阻断，dev 体验不变；生产已显式设置
不受影响）。

自检逻辑是纯函数 `jwtSecretInsecurityReason`（config.py），此处单测其三个分支；
另加 tokenize 骨架级装配断言（testapp-wiring-blindspot 的教训：main.py 的接线
集成测试结构性不可见，必须有守卫）。
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

from app import __file__ as appInitFile
from app.config import (
    DEV_JWT_SECRET_PLACEHOLDER,
    Settings,
    jwtSecretInsecurityReason,
)

_APP_DIR = Path(appInitFile).resolve().parent
_MAIN_PY = _APP_DIR / "main.py"


def test_placeholder_secret_is_flagged() -> None:
    reason = jwtSecretInsecurityReason(DEV_JWT_SECRET_PLACEHOLDER)
    assert reason is not None
    assert "占位" in reason
    assert "JWT_SECRET" in reason


def test_empty_secret_is_flagged() -> None:
    reason = jwtSecretInsecurityReason("")
    assert reason is not None
    assert "JWT_SECRET" in reason


def test_real_secret_passes() -> None:
    assert jwtSecretInsecurityReason("a" * 64) is None


def test_default_settings_secret_is_flagged() -> None:
    """缺省构造的 Settings 用的就是占位符 ⇒ 默认形态必须被自检逮到（这是本守卫存在的意义）。"""
    assert jwtSecretInsecurityReason(Settings().jwtSecret) is not None


def test_main_lifespan_wires_the_guard() -> None:
    """main.py 骨架（剥掉 STRING/COMMENT，防 docstring 误命中）必须引用自检函数。"""
    codeOnly = io.StringIO()
    with tokenize.open(str(_MAIN_PY)) as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            codeOnly.write(tok.string)
    assert "jwtSecretInsecurityReason" in codeOnly.getvalue(), (
        "main.py 未接线 jwtSecretInsecurityReason 启动自检（守卫在 config.py 里休眠）"
    )
