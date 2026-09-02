"""AgentBindingCache 模块行为测试（不接真实 DB，用 fake session）。"""
import pytest
from app.services.agent_binding_cache import AgentBindingCache


class _FakeRow:
    def __init__(self, code, tool):
        self.agent_code = code
        self.tool_name = tool
    def __iter__(self):
        yield self.agent_code
        yield self.tool_name


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows
    def all(self):
        return self._rows
    def scalar_one_or_none(self):
        return self._rows[0].tool_name if self._rows else None


class _FakeSession:
    def __init__(self, by_code):
        self.by_code = by_code  # dict[code, tool_name]
    async def execute(self, stmt):
        # 简化：根据 stmt 的 where 子句返回对应行
        s = str(stmt)
        if "WHERE agent_definition.tool_name IS NOT NULL" in s or "tool_name_updated_at" in s:
            rows = [self.by_code[c] for c in self.by_code]
            return _FakeResult(rows)
        # single refreshOne path
        return _FakeResult([])


@pytest.fixture
def cache():
    return AgentBindingCache()


@pytest.mark.asyncio
async def test_getToolName_raises_before_warmUp(cache):
    with pytest.raises(RuntimeError, match="未 warmUp"):
        cache.getToolName("ANY")


@pytest.mark.asyncio
async def test_warmUp_then_getToolName(cache):
    by_code = {
        "SUPPLIER_360_AGENT": _FakeRow("SUPPLIER_360_AGENT", "supplier_360"),
        "SUPPLIER_RISK_AGENT": _FakeRow("SUPPLIER_RISK_AGENT", "supplier_risk"),
    }
    await cache.warmUp(_FakeSession(by_code))
    assert cache.getToolName("SUPPLIER_360_AGENT") == "supplier_360"
    assert cache.getToolName("SUPPLIER_RISK_AGENT") == "supplier_risk"
    assert cache.getToolName("UNKNOWN") is None


@pytest.mark.asyncio
async def test_invalidate_single(cache):
    by_code = {"A": _FakeRow("A", "toolA")}
    await cache.warmUp(_FakeSession(by_code))
    cache.invalidate("A")
    assert cache.getToolName("A") is None


@pytest.mark.asyncio
async def test_invalidate_all(cache):
    by_code = {"A": _FakeRow("A", "toolA"), "B": _FakeRow("B", "toolB")}
    await cache.warmUp(_FakeSession(by_code))
    cache.invalidate()  # 全清
    assert cache.getToolName("A") is None
    assert cache.getToolName("B") is None