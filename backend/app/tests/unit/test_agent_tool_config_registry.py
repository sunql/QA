"""AgentToolConfigRegistry 单元测试。

- warmUp：bulk load enabled=True 行 → AgentTool（via Assembly）
- get/has/all：未 warmed → RuntimeError（lifespan bug）；warmed 后查
- invalidate(name|name=None)：写时失效（同步快路径）
- reload_one(session, name)：asyncio.Lock 保护的单行重载，DB miss → pop

不依赖真实 PG；用 MagicMock 模拟 ORM 行 + AsyncMock 模拟 session.execute。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.services.agent_tool_config_registry import AgentToolConfigRegistry
from app.services.agent_tools import AgentTool


@pytest.fixture
def reg():
    return AgentToolConfigRegistry()


def _rowMock(name: str, *, enabled: bool = True, **kw) -> MagicMock:
    defaults = dict(
        name=name, description="", data_object="SUPPLIER",
        data_layers=["DIM"], input_schema={},
        handler_kind="BUILTIN", handler_ref="supplier_360",
        arg_extractor_kind="supplier_key",
    )
    defaults.update(kw)
    defaults["enabled"] = enabled
    m = MagicMock()
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


def _asyncSessionWith(rows=None, one=None) -> MagicMock:
    """session.execute 链：scalars().all() → rows；scalar_one_or_none() → one。

    execute 是 async，但 return_value 必须是 MagicMock（同步链），否则
    `.scalars().all()` 会拿到 awaitable（AsyncMock 默认 chain）。
    """
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows or []
    result.scalar_one_or_none.return_value = one
    session.execute = AsyncMock(return_value=result)
    return session


class TestWarmUp:
    @pytest.mark.asyncio
    async def test_warmUp_filters_enabled_true_in_sql(self, reg):
        """验证 SELECT 含 WHERE enabled IS TRUE（mock 不会执行 filter，故断言 SQL）。"""
        session = _asyncSessionWith(rows=[])
        await reg.warmUp(session)
        stmt = session.execute.await_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "enabled" in compiled.lower()
        assert "true" in compiled.lower()

    @pytest.mark.asyncio
    async def test_warmUp_uses_assembly(self, reg):
        row = _rowMock(
            "supplier_360", description="x",
            data_layers=["DIM", "FEATURE"], input_schema={},
        )
        session = _asyncSessionWith(rows=[row])
        await reg.warmUp(session)
        tool = reg.get("supplier_360")
        assert tool is not None
        assert isinstance(tool, AgentTool)
        assert tool.name == "supplier_360"


class TestGetAndHas:
    def test_get_unwarmed_raises(self, reg):
        with pytest.raises(RuntimeError, match="未 warmUp"):
            reg.get("anything")

    def test_has_unwarmed_raises(self, reg):
        with pytest.raises(RuntimeError, match="未 warmUp"):
            reg.has("anything")

    def test_all_unwarmed_raises(self, reg):
        with pytest.raises(RuntimeError, match="未 warmUp"):
            reg.all()


class TestInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate_single_name(self, reg):
        rows = [_rowMock("r1"), _rowMock("r2")]
        session = _asyncSessionWith(rows=rows)
        await reg.warmUp(session)
        reg.invalidate("r1")
        assert reg.has("r1") is False
        assert reg.has("r2") is True

    def test_invalidate_all(self, reg):
        reg._loaded = True
        reg._tools = {"a": MagicMock(), "b": MagicMock()}
        reg.invalidate()
        assert reg._tools == {}

    def test_invalidate_unwarmed_is_safe_noop(self, reg):
        # 未 warmed 调用不应抛 — 是「快路径，不查 DB」的副作用
        reg.invalidate("anything")  # 不抛
        reg.invalidate()  # 不抛


class TestReloadOne:
    @pytest.mark.asyncio
    async def test_reload_one_inserts_when_db_has_row(self, reg):
        session = _asyncSessionWith(rows=[])
        await reg.warmUp(session)
        assert reg.has("new_tool") is False

        new_row = _rowMock(
            "new_tool", data_layers=["DIM"], input_schema={},
        )
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=new_row))
        )
        await reg.reload_one(session, "new_tool")
        assert reg.has("new_tool") is True

    @pytest.mark.asyncio
    async def test_reload_one_removes_when_db_missing(self, reg):
        rows = [_rowMock("a")]
        session = _asyncSessionWith(rows=rows)
        await reg.warmUp(session)
        assert reg.has("a") is True

        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )
        await reg.reload_one(session, "a")
        assert reg.has("a") is False

    @pytest.mark.asyncio
    async def test_reload_one_removes_when_disabled(self, reg):
        rows = [_rowMock("a", enabled=True)]
        session = _asyncSessionWith(rows=rows)
        await reg.warmUp(session)

        disabled_row = _rowMock("a", enabled=False)
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=disabled_row))
        )
        await reg.reload_one(session, "a")
        assert reg.has("a") is False


class TestAllSorted:
    @pytest.mark.asyncio
    async def test_all_returns_sorted(self, reg):
        rows = [_rowMock("z"), _rowMock("a"), _rowMock("m")]
        session = _asyncSessionWith(rows=rows)
        await reg.warmUp(session)
        assert [t.name for t in reg.all()] == ["a", "m", "z"]
