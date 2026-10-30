"""L4 冷启动 adapter 崩溃修复（2026-10-31 取证发现，main 既有 bug）。

缺陷：``_runL4AgentLoop`` 传 ``executor=self._adapterProvider(dto.datasourceId, None)``
（注释「懒加载」但 get_adapter 是立即调用的同步函数），``get_adapter`` 在进程内
``_adapters`` 缓存未命中时解引用 ``ds.password_encrypted`` → AttributeError →
L4 回退 L2/L3。历史 L4 之所以能用，是同进程内其它路径（L2/L3 查询、schema 内省）
先焐热了 adapter 池；**容器重启后第一次 L4 必失败**。

修复：与 ``chat_service._buildPipelineContext`` 同模式，先经
``self._datasource.get(session, datasourceId)`` 取真实 ds 行再传给
``_adapterProvider``；取 ds 失败走既有 try/except 降级路径。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.exceptions import DataSourceError
from app.services.chat_service import ChatService


def _makeService(
    *,
    ds: Any,
    dsError: Exception | None = None,
) -> tuple[ChatService, dict[str, list[Any]]]:
    """绕开 __init__ 构造 ChatService，spy 掉 _runL4AgentLoop 的协作方。"""
    svc = object.__new__(ChatService)
    calls: dict[str, list[Any]] = {"adapterProvider": [], "runAgentLoop": []}

    async def _getDatasource(_session: Any, datasourceId: int) -> Any:
        if dsError is not None:
            raise dsError
        return ds

    svc._datasource = type("_FakeDatasourceSvc", (), {"get": staticmethod(_getDatasource)})()

    def _adapterProvider(datasourceId: int, dsArg: Any) -> str:
        calls["adapterProvider"].append((datasourceId, dsArg))
        return "sentinel-executor"

    svc._adapterProvider = _adapterProvider  # type: ignore[method-assign]
    svc._ontology = object()  # type: ignore[attr-defined]

    async def _resolve(_session: Any, _dto: Any) -> tuple[str, Any]:
        config = type(
            "_FakeConfig", (), {"cost_per_1k_input": 0.001, "cost_per_1k_output": 0.002}
        )()
        return "sentinel-llm", config

    svc._resolveChatLlmClient = _resolve  # type: ignore[method-assign]

    async def _runAgentLoop(**kwargs: Any) -> Any:
        calls["runAgentLoop"].append(kwargs)
        return type(
            "_FakeLoopResult",
            (),
            {"prompt_tokens": 0, "completion_tokens": 0, "terminated_reason": "answered"},
        )()

    svc._agentRuntime = type("_FakeRuntime", (), {"run_agent_loop": staticmethod(_runAgentLoop)})()
    return svc, calls


def _dto() -> Any:
    from app.domain.schemas import ChatRequest

    return ChatRequest(
        sessionId="l4-cold-start-test", question="为什么5月份供货量差异大？", datasourceId=1
    )


class TestL4ColdStartAdapter:
    @pytest.mark.asyncio
    async def test_adapter_provider_receives_fetched_ds_not_none(self) -> None:
        """adapter 池冷缓存场景：必须把取到的 ds 传给 _adapterProvider（而非 None）。"""
        ds = type("_FakeDs", (), {"id": 1, "name": "THBI"})()
        svc, calls = _makeService(ds=ds)

        await svc._runL4AgentLoop(session=None, dto=_dto(), user=None)

        assert calls["adapterProvider"] == [(1, ds)], (
            "_adapterProvider 必须收到真实 ds（修复前传 None，冷缓存时 get_adapter 崩溃）"
        )
        assert calls["runAgentLoop"][0]["executor"] == "sentinel-executor"

    @pytest.mark.asyncio
    async def test_datasource_fetch_failure_falls_back_to_none(self) -> None:
        """取 ds 失败（数据源不存在等）→ 返回 None 降级 L2/L3，不向上抛。"""
        svc, calls = _makeService(ds=None, dsError=DataSourceError("数据源不存在"))

        result = await svc._runL4AgentLoop(session=None, dto=_dto(), user=None)

        assert result is None
        assert calls["runAgentLoop"] == [], "取 ds 失败不应再进 agent loop"
