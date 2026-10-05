"""执行期模型直选单测（feat-research-entry-ux-fixes W5-b）。

纯单测、零 DB：`resolveModelConfig` 需要的 session 只用于 `begin_nested()` 与
`buildRoutingContext`（后者在 tokenUsage=None 时提前返回，不碰 DB）——
故用两个极简替身即可覆盖全部分支。

契约要点（对应用户反馈第 1 条）：
- 指定模型 ⇒ **直选**，不进路由器；
- 指定模型按**全量**清单找（存在但停用 ⇒ 报错，而不是被可用池过滤后误判为不存在）；
- 指定模型 key 缺失 / 不存在 ⇒ **显式报错，绝不静默换模型**；
- 未指定 ⇒ 现有自动路由行为逐字不变。

另含**接线**单测（fix round 1）：`state["modelId"]` → `resolveClient(preferredModelId=...)`
的透传值断言（只断言「调用发生」会漏掉接线断开，故断言**到达 ports 的值**）。
"""

from typing import Any

import pytest
import pytest_asyncio

from app.services import research_agent_service
from app.services.research_agent_ports import (
    PreferredModelUnavailableError,
    resolveModelConfig,
)
from app.services.research_agent_service import ResearchAgentService

# ---------------------------------------------------------------------------
# 本文件是纯函数测试，不需要 DB。
# 覆盖 conftest 的 autouse DB fixtures —— 否则每个用例都会 TRUNCATE 整个测试库
# （seedEngine 里 _truncateAll），既慢又会抹掉集成测试依赖的迁移种子数据。
# ---------------------------------------------------------------------------


@pytest.fixture()
def dbSession() -> Any:
    """覆盖 conftest autouse dbSession —— 本文件不用 DB。"""
    return None


@pytest_asyncio.fixture()
async def seedEngine() -> Any:
    """覆盖 conftest autouse seedEngine —— 不建引擎、不 truncate。"""
    return None


@pytest_asyncio.fixture()
async def warmBusinessObjectRegistry() -> Any:
    """覆盖 conftest autouse warmBusinessObjectRegistry —— 无需 DB。"""
    yield


class _FakeConfig:
    def __init__(self, configId: int, *, isActive: bool = True, hasKey: bool = True) -> None:
        self.id = configId
        self.is_active = isActive
        self._hasKey = hasKey


class _FakeConfigs:
    """按 activeOnly 返回不同清单（显式选择走全量，自动路由走可用池）。"""

    def __init__(self, configs: list[_FakeConfig]) -> None:
        self._configs = configs
        self.calls: list[bool] = []

    async def list(self, session: Any, *, activeOnly: bool = False) -> list[_FakeConfig]:
        self.calls.append(activeOnly)
        if activeOnly:
            return [config for config in self._configs if config.is_active]
        return list(self._configs)


def _factory(config: Any) -> Any:
    """`buildClient` 的替身：有 key 的配置返回哨兵客户端，无 key 返回 None。"""
    return object() if getattr(config, "_hasKey", False) else None


class _FakeRouter:
    def __init__(self) -> None:
        self.calls = 0

    def selectModel(self, configs: list[Any], prompt: str, ctx: Any) -> Any:
        self.calls += 1
        return configs[0]


class _FakeNested:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeSession:
    """`resolveModelConfig` 用 `session.begin_nested()` 隔离配置读失败；单测无需真库。"""

    def begin_nested(self) -> _FakeNested:
        return _FakeNested()


async def test_preferred_model_is_selected_directly_without_router() -> None:
    configs = _FakeConfigs([_FakeConfig(1), _FakeConfig(2)])
    router = _FakeRouter()

    picked = await resolveModelConfig(
        configs, router, _factory, _FakeSession(),
        question="q", sessionId="s", preferredModelId=2,
    )

    assert picked.id == 2
    assert router.calls == 0


async def test_preferred_model_reads_full_list_not_usable_pool() -> None:
    """存在但已停用 ⇒ 报错，而不是被「可用池」过滤后误判为不存在。"""
    configs = _FakeConfigs([_FakeConfig(1, isActive=False)])

    with pytest.raises(PreferredModelUnavailableError):
        await resolveModelConfig(
            configs, _FakeRouter(), _factory, _FakeSession(),
            question="q", sessionId="s", preferredModelId=1,
        )

    assert configs.calls == [False]


async def test_preferred_model_missing_raises() -> None:
    with pytest.raises(PreferredModelUnavailableError):
        await resolveModelConfig(
            _FakeConfigs([_FakeConfig(1)]), _FakeRouter(), _factory, _FakeSession(),
            question="q", sessionId="s", preferredModelId=99,
        )


async def test_preferred_model_without_key_raises_instead_of_substituting() -> None:
    """选中的模型无 API key：显式报错，绝不静默换模型。"""
    with pytest.raises(PreferredModelUnavailableError):
        await resolveModelConfig(
            _FakeConfigs([_FakeConfig(1, hasKey=False)]), _FakeRouter(), _factory,
            _FakeSession(), question="q", sessionId="s", preferredModelId=1,
        )


async def test_no_preference_still_uses_router() -> None:
    """未指定 ⇒ 自动路由行为逐字不变。"""
    configs = _FakeConfigs([_FakeConfig(1), _FakeConfig(2)])
    router = _FakeRouter()

    picked = await resolveModelConfig(
        configs, router, _factory, _FakeSession(), question="q", sessionId="s"
    )

    assert picked.id == 1
    assert router.calls == 1
    assert configs.calls == [True]


async def test_no_preference_and_no_usable_config_returns_none() -> None:
    configs = _FakeConfigs([_FakeConfig(1, hasKey=False)])

    assert (
        await resolveModelConfig(
            configs, _FakeRouter(), _factory, _FakeSession(), question="q", sessionId="s"
        )
        is None
    )


# ---------------------------------------------------------------------------
# 接线（W5-b fix round 1）：state["modelId"] → ports.resolveClient 的 preferredModelId
#
# 上面 6 条只证明 ports 收到参数后行为正确；若服务侧接线断了（state 里没值 / 没透传），
# 它们全绿而线上仍会静默换模型 —— 本组用例堵这个盲区。
# ---------------------------------------------------------------------------


class _FakeReporter:
    """`reporter` 端口替身：不注入则构造真实 ReportPlanner（本组用例用不到报告）。"""


class _ProbeConfigs:
    """配置端口占位：`resolveClient` 已被探针替换，本端口不会被读到。"""


def _makeService() -> ResearchAgentService:
    """服务实例（构造期零 DB）：协作者全传替身，默认协作件均为惰性对象。"""
    return ResearchAgentService(
        esl=object(),
        sessionService=object(),
        planner=object(),
        runner=object(),
        chartService=object(),
        reporter=_FakeReporter(),
        modelConfigs=_ProbeConfigs(),
        modelRouter=_FakeRouter(),
        tokenUsage=object(),
    )


def _forwardingProbe(forwarded: list[Any]):
    """替身 `resolveClient`：签名与 ports 同形，记录到达的 preferredModelId。"""

    async def _probe(
        factory: Any, modelConfigs: Any, modelRouter: Any, session: Any, *,
        state: dict[str, Any], emit: Any, sessionId: Any,
        tokenUsage: Any = None, preferredModelId: int | None = None,
    ) -> tuple[Any, Any]:
        forwarded.append(preferredModelId)
        return ("client", "config")

    return _probe


async def test_resolve_client_forwards_session_model_id_to_ports(monkeypatch: Any) -> None:
    """会话选定模型 ⇒ `preferredModelId` 必须是**该 id**（不是 None、不是只发生调用）。"""
    forwarded: list[Any] = []
    monkeypatch.setattr(research_agent_service, "resolveClient", _forwardingProbe(forwarded))
    svc = _makeService()

    client, config = await svc._resolveClient(
        object(), state={"modelId": 42}, emit=None, sessionId="s"
    )

    assert (client, config) == ("client", "config")  # 返回值原样透传
    assert forwarded == [42]


async def test_resolve_client_forwards_none_when_session_has_no_model(monkeypatch: Any) -> None:
    """未选定（会话 model_id 为 NULL）⇒ 传 None，保持自动路由。"""
    forwarded: list[Any] = []
    monkeypatch.setattr(research_agent_service, "resolveClient", _forwardingProbe(forwarded))
    svc = _makeService()

    await svc._resolveClient(object(), state={}, emit=None, sessionId="s")

    assert forwarded == [None]
