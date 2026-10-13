"""main.py 的 lifespan 真的接线了启动回收 reaper（Task 10+11 follow-up）。

**为什么需要这个文件**：`app/tests/integration/research/test_research_task.py` 的 7 条
用例全部**直接调** ``reapOrphanedTasks``，**绕过** ``main.py`` 的 lifespan ⇒ 把
``main.py`` 里那整块 reaper（import + 调用 + commit）删掉，**没有任何测试会红**。
这是本仓反复被咬的形状：能力在、接线零覆盖（``testapp-wiring-blindspot``）。本文件补上
骨架级装配守卫，与 ``test_jwt_secret_startup_guard.py`` /
``test_production_auth_guard.py`` 的 ``test_main_lifespan_wires_the_guard`` 同款范式。

**手段**：读 ``main.py`` 源码文本，用 ``tokenize`` 剥掉 STRING / COMMENT 后断言符号
出现在**代码** token 流里（防 docstring / 注释误命中）。两条断言分别覆盖：
1. 名字出现（删掉整块 ⇒ 红）；
2. 真的以 ``await reapOrphanedTasks(...)`` **调用**（只删调用、留下 import ⇒ 也红）。
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

from app import __file__ as appInitFile

_APP_DIR = Path(appInitFile).resolve().parent
_MAIN_PY = _APP_DIR / "main.py"


def _mainPyCodeOnly() -> str:
    """拼接 ``main.py`` 的 STRING / COMMENT 之外的 token（防 docstring/注释误命中）。

    token 之间不插分隔符，故源码里的 ``await reapOrphanedTasks(`` 会连成
    ``awaitreapOrphanedTasks(`` —— 调用点断言即据此（与换行/缩进无关，稳）。
    """
    codeOnly = io.StringIO()
    with tokenize.open(str(_MAIN_PY)) as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            codeOnly.write(tok.string)
    return codeOnly.getvalue()


def test_main_lifespan_references_the_reaper() -> None:
    """名字必须在 ``main.py`` 代码 token 流里出现（删掉整块 reaper ⇒ 本用例红）。"""
    code = _mainPyCodeOnly()
    assert "reapOrphanedTasks" in code, (
        "main.py 未接线 reapOrphanedTasks 启动回收"
        "（能力在 research_task_service 里休眠，崩溃会话的 active_run_id 无人释放）"
    )


def test_main_lifespan_awaits_the_reaper() -> None:
    """必须是 ``await reapOrphanedTasks(...)`` 调用，而非只 import 不用（只看调用点 ⇒ 红）。"""
    code = _mainPyCodeOnly()
    assert "awaitreapOrphanedTasks(" in code, (
        "main.py 只引用了 reapOrphanedTasks 却未 ``await`` 调用它"
        "（import 有、调用被删 ⇒ 回收从不发生）"
    )
