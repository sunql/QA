"""Task 7 集成测试（入口模块）：编排运行的 route 可用性（feature flag）。

本文件只保留「feature flag 关闭时整组路由不存在（404）」这一个用例；
其余 run 生命周期用例按 subject 拆分到同目录的兄弟模块：

- [[test_agent_runs_api_create.py]]：create（202 QUEUED / 快照 / 失败路径）
- [[test_agent_runs_api_list.py]]：list（过滤、limit/offset、只返回自己的 run）
- [[test_agent_runs_api_read_acl.py]]：读 ACL（他人的 run 一律 404，admin 可读）
- [[test_agent_runs_api_steps.py]]：steps（stepNo 升序、afterStepNo 生效）
- [[test_agent_runs_api_resume.py]]：resume（暂停原因 / 预算增量 / 权限复查）
- [[test_agent_runs_api_cancel.py]]：cancel（QUEUED/PAUSED/RUNNING/终态）
- [[test_agent_runs_api_history_audit.py]]：历史数据 + 审计 outbox

共享的常量、client 夹具与造数助手在 `_agent_runs_api_helpers.py`
（`_` 前缀，pytest 不收集）。

所有 Agent / 工具 / profile / 模型均通过既有公开 API 创建；PAUSED / RUNNING 等
中间态 run 直接落库造数（真实 PostgreSQL 行）。
"""

from __future__ import annotations

from app.config import getSettings
from app.tests import _pg_support
from app.tests.integration._agent_runs_api_helpers import (
    _AGENT_CODE,
    _BASE_PATH,
    OWNER_HEADERS,
)

# ---------------------------------------------------------------------------
# feature flag
# ---------------------------------------------------------------------------


async def test_disabled_feature_returns_404(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_AGENT_ORCHESTRATION", "false")
    getSettings.cache_clear()
    try:
        async for ac in _pg_support.pgApiClient():
            listing = await ac.get(_BASE_PATH, headers=OWNER_HEADERS)
            created = await ac.post(
                _BASE_PATH,
                headers=OWNER_HEADERS,
                json={
                    "agentCode": _AGENT_CODE,
                    "input": "hi",
                    "modelConfigId": 1,
                },
            )
    finally:
        getSettings.cache_clear()
    assert listing.status_code == 404
    assert created.status_code == 404
    # 必须是「路由不存在」的 404（FastAPI 默认体），而不是领域异常的 404
    assert listing.json() == {"detail": "Not Found"}
    assert created.json() == {"detail": "Not Found"}
