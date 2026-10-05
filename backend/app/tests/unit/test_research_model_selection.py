"""执行期模型直选单测（feat-research-entry-ux-fixes W5-b）。

纯单测、零 DB：`resolveModelConfig` 需要的 session 只用于 `begin_nested()` 与
`buildRoutingContext`（后者在 tokenUsage=None 时提前返回，不碰 DB）——
故用两个极简替身即可覆盖全部分支。

契约要点（对应用户反馈第 1 条）：
- 指定模型 ⇒ **直选**，不进路由器；
- 指定模型按**全量**清单找（存在但停用 ⇒ 报错，而不是被可用池过滤后误判为不存在）；
- 指定模型 key 缺失 / 不存在 ⇒ **显式报错，绝不静默换模型**；
- 未指定 ⇒ 现有自动路由行为逐字不变。
"""

from typing import Any

import pytest
import pytest_asyncio

from app.services.research_agent_ports import (
    PreferredModelUnavailableError,
    resolveModelConfig,
)

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
