"""Task 8 单元：``loadStepResultsByTurn`` 读失败降级为空。

报告不得因读步结果而炸（对齐 ``_loadArms`` 的 ``begin_nested`` + try/except 口径）：
表缺 / 连接坏等读失败 ⇒ ``logger.warning`` + 返回 ``[]``。此处用最小替身 session 直接
触发 ``begin_nested()`` 抛错，覆盖降级分支（真库路径见
``integration/research/test_step_result_by_turn.py``）。
"""

from __future__ import annotations

import uuid

import pytest

from app.services.research_step_result import loadStepResultsByTurn

pytestmark = pytest.mark.asyncio


class _BoomSession:
    """``begin_nested()`` 直接抛：模拟表缺 / 连接坏等读失败。"""

    def begin_nested(self) -> object:
        raise RuntimeError("步结果表不可读")


async def test_load_step_results_by_turn_degrades_to_empty_on_read_failure() -> None:
    rows = await loadStepResultsByTurn(_BoomSession(), uuid.uuid4())  # type: ignore[arg-type]
    assert rows == []
