# Implementation Plan: Chat 接入 MCP — DeepSeek 原生 Tool Use 子集

> **本文档是 plan.md 的 DeepSeek-only 子集实施方案**
>
> 原 plan.md 是通用方案（覆盖国产降级 + OpenAI 兼容 + Context7 + microsoft-learn），
> 本计划聚焦**只用 DeepSeek 作为 MCP 支撑大模型**的最小可行路径，
> 砍掉 XML 降级分支 / GLM / Kimi 等未实测 provider，节省约 57% 代码量。
>
> 关联：
> - [plan.md](./plan.md) — 通用方案（MCP 协议接入总纲）
> - [research-sdk.md](./research-sdk.md) — MCP python-sdk 调研（锁 `mcp>=1.30.0,<2`）
> - [research-cn-models.md](./research-cn-models.md) — 国产模型实测（DeepSeek 走原生）
> - [research-context7.md](./research-context7.md) — Context7 schema 实地验证（snake_case / silent redirect / is_error 不可信）
> - [research-migration.md](./research-migration.md) — alembic 0084 mcp_call_log

---

## 1. 为什么选 DeepSeek 而不是国产降级

| 维度 | DeepSeek 原生 | Qwen3.6 JSON 降级（plan §4.2.B） |
|---|---|---|
| tool_call 协议 | OpenAI 兼容，`tools` 参数原生 | prompt 注入 XML 块 + JSON 解析 |
| 解析成功率 | ~100%（SDK 自动） | ~70%（任务 12 实测，临界达标） |
| fence 预处理 | 不需要（SDK 解析） | **必须**先剥 ` ```json ``` ` 再 fullmatch |
| args 校验 | SDK 自动 | 需 jsonschema 手写 |
| 错误判定 | `tool_calls` 为空 | `re.fullmatch(r'\{...\}')` 失败回退 |
| **代码量** | ~600 行 | ~1400 行（plan 全量） |

**结论**：DeepSeek 走原生是性价比最高、最稳定的路径。**XML 降级路径暂时不实现**，留 TODO，等真要用 Qwen/GLM 时按 plan §4.2.B 补即可。

---

## 2. 3 个前置验证（Step 0，每个 5 分钟）

### V1. 当前 DeepSeek 是否真在跑 + modelId

```bash
docker exec qa-postgres psql -U postgres -d qa_metadata -c "
  SELECT id, code, display_name, weight, is_active, base_url, model_name
  FROM llm_config
  WHERE code ILIKE '%deepseek%' OR model_name ILIKE '%deepseek%';
"
```

**断言**：
- `is_active=true` 且 `weight>0`（否则 `_weightedChoice` 选不到）
- `base_url` 应为 `https://api.deepseek.com/v1`
- `model_name` **必须是 `deepseek-chat`**（不能是 `deepseek-reasoner`，reasoner **不支持 tool_use**）

如果不满足，先调权重 / 激活 / 切 model。

### V2. DeepSeek API key 是否在容器内

```bash
docker exec qa-backend env | grep -iE "(deepseek|api_key|llm_)" | head -20
```

或查 system_config / llm_config 表里 api_key 字段（可能是密文，看 `app/services/secret_service.py` 怎么解）。

### V3. 真机验证 DeepSeek tool_calls 回包

写一个最小 Python 脚本 `/tmp/deepseek_tool_test.py`：

```python
import asyncio
from openai import AsyncOpenAI

async def main():
    client = AsyncOpenAI(
        api_key="<从 env 或 secret 解出来>",
        base_url="https://api.deepseek.com/v1",
    )
    resp = await client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": "北京今天天气如何？请调用 get_weather 工具"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查指定城市天气",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string", "description": "城市名"}},
                    "required": ["city"],
                },
            },
        }],
        tool_choice="auto",
        max_tokens=1024,
    )
    msg = resp.choices[0].message
    print("content:", msg.content)
    print("tool_calls:", msg.tool_calls)
    if msg.tool_calls:
        tc = msg.tool_calls[0]
        print(f"  name: {tc.function.name}")
        print(f"  arguments: {tc.function.arguments}")

asyncio.run(main())
```

**断言**：
- `msg.tool_calls` 不为 None 且长度 ≥ 1
- `tc.function.name == "get_weather"`
- `tc.function.arguments` 是合法 JSON 字符串

**失败排查**：
- 若 `tool_calls is None`：模型是 reasoner 或 tools 参数序列化错
- 若 name 错乱：tools 缺 `type: function`
- 若 arguments 不是 JSON：检查 schema 是否合法

---

## 3. 实施路径（6 个 commit，TDD 优先）

### Commit 1：factory.py 标记 DeepSeek 支持原生 tool_use

**文件**：`app/infrastructure/llm/factory.py`

```python
# 既有 imports 不变，新增 frozenset 和查询函数
_NATIVE_TOOL_USE_PROVIDERS = frozenset({
    ProviderType.OPENAI,
    ProviderType.AZURE_OPENAI,
    ProviderType.DEEPSEEK,                # ★ 新增
    ProviderType.OPENAI_COMPATIBLE_PROXY,  # 既有
})
# Qwen / GLM / Ollama / reasoner 不在内 → 走 prompt 注入（本期不实现）

def supports_native_tool_use(provider: ProviderType) -> bool:
    return provider in _NATIVE_TOOL_USE_PROVIDERS
```

**先确认** `ProviderType.DEEPSEEK` 是否已在 `app/domain/llm_types.py`，没有就加。

**测试**（`tests/unit/test_factory_tool_use.py`）：
```python
def test_supports_native_tool_use_deepseek():
    assert supports_native_tool_use(ProviderType.DEEPSEEK) is True

def test_supports_native_tool_use_qwen_excluded():
    assert supports_native_tool_use(ProviderType.QWEN) is False  # 假设有该枚举
```

### Commit 2：MCPClientManager + lifespan 接入

**新文件**：`app/services/mcp_client_manager.py`（~250 行）

**职责**：stdio 子进程连接 + 工具发现缓存 + 熔断器。

**关键设计**（来自任务 13 实测）：
- ✅ stdio 冷启动 ~2.2s/服务器 → **lazy spawn**（首次调用时连接，不在 lifespan 阻塞）
- ✅ 错误三层捕获：`McpError`（协议）/ `result.is_error`（工具）/ `ConnectionError+TimeoutError`（transport）
- ✅ Context7 不需要 API key → `api_key` 字段可选
- ✅ `tool.input_schema` snake_case（Pydantic 暴露），不是 `inputSchema` camelCase

**核心代码骨架**：
```python
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, McpError, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class MCPServerConfig(BaseModel):
    name: str
    transport: str = "stdio"  # 未来支持 "streamable_http"
    command: list[str]
    env: dict[str, str] | None = None
    enabled: bool = True
    tool_priority: str = "normal"  # high / normal / low


class MCPCachedTool(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]   # ⚠️ snake_case 来自 mcp Python 包
    server_name: str


class MCPUnavailableError(Exception):
    """MCP server 不可用（熔断 / 超时 / 连接失败）"""


class MCPTimeoutError(MCPUnavailableError):
    pass


class CircuitBreaker:
    def __init__(self, failure_threshold=5, reset_timeout_seconds=60):
        self._failures = 0
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout_seconds
        self._opened_at: float | None = None

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if asyncio.get_event_loop().time() - self._opened_at > self._reset_timeout:
            self._failures = 0
            self._opened_at = None
            return False
        return True

    def record_failure(self):
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = asyncio.get_event_loop().time()
            logger.warning(f"Circuit breaker opened after {self._failures} failures")

    def record_success(self):
        self._failures = max(0, self._failures - 1)


class MCPClientManager:
    def __init__(self, configs: list[MCPServerConfig]):
        self._configs = {c.name: c for c in configs if c.enabled}
        self._sessions: dict[str, ClientSession] = {}
        self._tool_cache: dict[str, list[MCPCachedTool]] = {}
        self._breakers: dict[str, CircuitBreaker] = {}

    async def warmup(self) -> None:
        """懒启动：仅连接 + list_tools，不强制"""
        for name, cfg in self._configs.items():
            try:
                await self._ensure_session(name)
            except Exception as exc:
                logger.warning(f"MCP server {name} warmup failed: {exc}")

    async def _ensure_session(self, server_name: str) -> ClientSession:
        if server_name in self._sessions:
            return self._sessions[server_name]
        cfg = self._configs[server_name]
        params = StdioServerParameters(
            command=cfg.command[0],
            args=cfg.command[1:],
            env=cfg.env,
        )
        # stdio_client 是 async context manager，需注意生命周期
        # 这里用 lazy lazy：在 call_tool 时才真正连
        self._sessions[server_name] = await self._connect(cfg)
        # list_tools
        tools_result = await self._sessions[server_name].list_tools()
        self._tool_cache[server_name] = [
            MCPCachedTool(
                name=t.name,
                description=t.description or "",
                input_schema=t.input_schema,  # ⚠️ snake_case
                server_name=server_name,
            )
            for t in tools_result.tools
        ]
        return self._sessions[server_name]

    async def _connect(self, cfg: MCPServerConfig) -> ClientSession:
        """实际 stdio 连接 + initialize"""
        params = StdioServerParameters(
            command=cfg.command[0],
            args=cfg.command[1:],
            env=cfg.env,
        )
        # 用持久 stream，存进 self 以便 close
        read, write = await stdio_client(params)
        session = ClientSession(read, write)
        await session.initialize()
        return session

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        breaker = self._breakers.setdefault(server_name, CircuitBreaker())
        if breaker.is_open():
            raise MCPUnavailableError(f"{server_name} circuit open")

        try:
            session = await self._ensure_session(server_name)
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=timeout_seconds,
            )
            # ⚠️ 任务 13 教训：错误判定不能只看 is_error
            if result.is_error:
                raise MCPUnavailableError(f"tool returned error: {result.content}")
            breaker.record_success()
            # content 可能是 list[TextContent | ...]，统一提取文本
            text_parts = [
                c.text for c in result.content
                if hasattr(c, "text") and c.text
            ]
            return {"text": "\n".join(text_parts), "raw": result.model_dump()}
        except (asyncio.TimeoutError, ConnectionError, McpError) as exc:
            breaker.record_failure()
            logger.warning(f"MCP {server_name}.{tool_name} failed: {exc}")
            raise MCPTimeoutError(str(exc)) from exc

    def list_all_tools(self) -> list[MCPCachedTool]:
        """返回所有 server 的工具列表（已缓存）"""
        tools: list[MCPCachedTool] = []
        for tl in self._tool_cache.values():
            tools.extend(tl)
        return tools

    async def shutdown(self) -> None:
        for s in self._sessions.values():
            try:
                await s.close()
            except Exception:
                pass
        self._sessions.clear()
        self._tool_cache.clear()
```

**测试**（`tests/integration/test_mcp_client_manager.py`，**真 lifespan + mock subprocess**）：

```python
import pytest
from app.services.mcp_client_manager import (
    MCPClientManager, MCPServerConfig, MCPUnavailableError,
)


@pytest.fixture
async def fake_context7_server():
    """起一个最小 mock Context7 stdio server"""
    # 实现见 tests/conftest.py:make_fake_mcp_server()
    ...


async def test_warmup_connects_and_caches_tools(fake_context7_server):
    cfg = MCPServerConfig(
        name="context7",
        command=fake_context7_server.cmd,
        enabled=True,
    )
    mgr = MCPClientManager([cfg])
    await mgr.warmup()
    tools = mgr.list_all_tools()
    assert any(t.name == "query-docs" for t in tools)


async def test_call_tool_returns_text(fake_context7_server):
    cfg = MCPServerConfig(name="context7", command=fake_context7_server.cmd)
    mgr = MCPClientManager([cfg])
    result = await mgr.call_tool(
        "context7", "query-docs",
        {"libraryName": "fastapi", "query": "Depends"},
    )
    assert "text" in result
    assert "Depends" in result["text"] or "FastAPI" in result["text"]


async def test_circuit_breaker_opens_after_threshold(fake_context7_server):
    cfg = MCPServerConfig(name="context7", command=fake_context7_server.cmd)
    mgr = MCPClientManager([cfg])
    # mock server 强制返回错误 6 次
    fake_context7_server.force_error(6)
    for _ in range(5):
        with pytest.raises(MCPUnavailableError):
            await mgr.call_tool("context7", "query-docs", {})
    # 第 6 次直接熔断，不再调真实连接
    with pytest.raises(MCPUnavailableError, match="circuit open"):
        await mgr.call_tool("context7", "query-docs", {})
```

### Commit 3：MCPAdapter + AgentTool 注册

**新文件**：`app/services/mcp_adapter.py`（~180 行）

**职责**：list_tools 缓存 → 转为 AgentTool 实例注入 AgentToolRegistry + OpenAI tools 数组构造。

**关键决策**（来自 plan §3 边界设计）：
- MCP 工具**伪装成 AgentTool** 注入注册表，`data_object=MCP_<SERVER>`，复用 ACL 五道闸
- OpenAI tools 数组只对 DeepSeek / OpenAI 路径生成；其他 provider 留 stub（不实现 XML 降级）

```python
from app.services.agent_tool_types import AgentTool, ToolResult
from app.services.mcp_client_manager import MCPClientManager, MCPCachedTool


def _tool_name_to_agent_code(tool_name: str) -> str:
    """mcp:context7:query-docs → MCP_CONTEXT7_QUERY_DOCS_AGENT"""
    parts = tool_name.replace("mcp:", "").replace("-", "_").split(":")
    return "_".join(p.upper() for p in parts) + "_AGENT"


def _to_openai_tool(tool: MCPCachedTool) -> dict:
    """MCP tool → OpenAI function calling schema"""
    return {
        "type": "function",
        "function": {
            "name": f"mcp:{tool.server_name}:{tool.name}",
            "description": tool.description,
            "parameters": tool.input_schema,  # 直接透传
        },
    }


class MCPAdapter:
    def __init__(self, manager: MCPClientManager, max_prompt_tools: int = 12):
        self._mgr = manager
        self._max = max_prompt_tools

    async def hydrate_agent_registry(self, registry) -> int:
        """把 MCP 工具注册成 AgentTool，返回注册数量"""
        tools = self._mgr.list_all_tools()
        registered = 0
        for tool in tools:
            full_name = f"mcp:{tool.server_name}:{tool.name}"
            agent_code = _tool_name_to_agent_code(full_name)
            data_object = f"MCP_{tool.server_name.upper()}"

            agent_tool = AgentTool(
                name=full_name,
                description=tool.description,
                data_object=data_object,
                data_layers=(),
                input_schema=tool.input_schema,
                arg_extractor=self._build_arg_extractor(tool),
                handler=self._make_dispatch_handler(tool.server_name, tool.name),
            )
            registry.register(agent_tool)
            registered += 1
        return registered

    def build_openai_tools_block(self) -> list[dict] | None:
        """仅 DeepSeek / OpenAI 用"""
        tools = self._rank_tools()[:self._max]
        return [_to_openai_tool(t) for t in tools]

    def _rank_tools(self) -> list[MCPCachedTool]:
        """按 priority + 时间衰减排序"""
        # 简化版：先 high → normal → low
        priority_order = {"high": 0, "normal": 1, "low": 2}
        cfg_map = {c.name: c.tool_priority for c in self._mgr._configs.values()}
        return sorted(
            self._mgr.list_all_tools(),
            key=lambda t: priority_order.get(cfg_map.get(t.server_name, "normal"), 1),
        )

    def _build_arg_extractor(self, tool: MCPCachedTool):
        """从 LLM tool_call.arguments 字符串解析 args"""
        def extractor(arguments_str: str) -> dict:
            import json
            try:
                return json.loads(arguments_str)
            except json.JSONDecodeError:
                return {}
        return extractor

    def _make_dispatch_handler(self, server_name: str, tool_name: str):
        """转发到 MCPClientManager.call_tool"""
        async def handler(session, args, ctx):
            from app.services.mcp_client_manager import MCPUnavailableError
            try:
                result = await self._mgr.call_tool(server_name, tool_name, args)
                return ToolResult(
                    data={"text": result["text"]},
                    answer=result["text"][:500],  # 截断喂回 LLM
                    tokens_used=0,
                    cost=0.0,
                    llm_model_name="",
                )
            except MCPUnavailableError as exc:
                # 降级：返回错误信息而非 5xx，让 LLM 决定下一步
                return ToolResult(
                    data={"error": str(exc)},
                    answer=f"工具 {tool_name} 暂时不可用：{exc}",
                    tokens_used=0,
                    cost=0.0,
                    llm_model_name="",
                )
        return handler
```

**测试**（`tests/unit/test_mcp_adapter.py`）：
```python
def test_tool_name_to_agent_code():
    assert _tool_name_to_agent_code("mcp:context7:query-docs") == "MCP_CONTEXT7_QUERY_DOCS_AGENT"
    assert _tool_name_to_agent_code("mcp:microsoft-learn:search") == "MCP_MICROSOFT_LEARN_SEARCH_AGENT"


def test_to_openai_tool():
    tool = MCPCachedTool(
        name="query-docs",
        description="查文档",
        input_schema={"type": "object", "properties": {"libraryName": {"type": "string"}}},
        server_name="context7",
    )
    result = _to_openai_tool(tool)
    assert result["type"] == "function"
    assert result["function"]["name"] == "mcp:context7:query-docs"
    assert "inputSchema" not in result["function"]["parameters"]


async def test_build_openai_tools_block_respects_max():
    # 注入 20 个 tool，max=12，断言只输出 12 个
    ...


async def test_arg_extractor_handles_invalid_json():
    extractor = MCPAdapter._build_arg_extractor(None, None)
    assert extractor("not json") == {}
    assert extractor('{"a": 1}') == {"a": 1}
```

### Commit 4：ChatMCPExecutor（DeepSeek 原生 tool_call 解析）

**新文件**：`app/services/chat_mcp_executor.py`（~100 行，比 plan §5.3 简 50 行，因为不走 JSON 降级）

```python
import json
import logging
from typing import Any

from openai.types.chat import ChatCompletionMessage

from app.domain.errors import DomainError
from app.infrastructure.llm.base_client import BaseLlmClient, LlmMessage
from app.services.agent_runtime_service import AgentRuntimeService
from app.services.mcp_adapter import MCPAdapter, _tool_name_to_agent_code

logger = logging.getLogger(__name__)


class ChatMCPExecutor:
    def __init__(
        self,
        *,
        llm_factory,
        adapter: MCPAdapter,
        runtime: AgentRuntimeService,
        max_iter: int = 3,
    ):
        self._llm_factory = llm_factory
        self._adapter = adapter
        self._runtime = runtime
        self._max_iter = max_iter

    async def run_loop(
        self,
        session,
        messages: list[dict],
        *,
        model_id: int,
        actor,
    ) -> list[dict]:
        """循环：LLM → 若有 tool_calls → 路由 AgentRuntime → 回填 → 继续"""
        appended: list[dict] = []
        tools_block = self._adapter.build_openai_tools_block()

        for i in range(self._max_iter):
            resp = await self._llm_call(messages + appended, model_id, tools_block)
            msg = resp.choices[0].message

            # 无 tool_call → 终止，把 assistant 消息追加
            if not msg.tool_calls:
                appended.append({
                    "role": "assistant",
                    "content": msg.content or "",
                })
                break

            # 有 tool_call → 先追加 assistant（含 tool_calls）
            appended.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })

            # 逐个 tool_call 路由到 AgentRuntime
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError as exc:
                    appended.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({"error": f"invalid JSON: {exc}"}, ensure_ascii=False),
                    })
                    continue

                agent_code = _tool_name_to_agent_code(tc.function.name)
                try:
                    result = await self._runtime.run(
                        session,
                        agent_code=agent_code,
                        args=args,
                        actor=actor,  # ⚠️ 必填，deny-by-default
                        llm_factory=self._llm_factory,
                    )
                    content = json.dumps(result.data, ensure_ascii=False)
                except DomainError as exc:
                    content = json.dumps({"error": str(exc)}, ensure_ascii=False)

                appended.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": content,
                })

        return appended

    async def _llm_call(self, messages, model_id, tools_block):
        """封一层方便测试时 mock"""
        client = self._llm_factory.create_client(model_id)
        return await client.chat.completions.create(
            model=client.model_name,
            messages=messages,
            tools=tools_block,
            tool_choice="auto",
            max_tokens=1024,  # 任务 12 教训
            stream=False,
        )
```

**测试**（`tests/integration/test_chat_mcp_executor.py`）：
```python
async def test_no_tool_calls_breaks_loop():
    """DeepSeek 返回 content 但无 tool_calls → 立即终止"""
    executor = ChatMCPExecutor(...)
    # mock _llm_call 返回无 tool_calls
    result = await executor.run_loop(session, [], model_id=1, actor=actor)
    assert len(result) == 1
    assert result[0]["role"] == "assistant"


async def test_tool_call_dispatched_through_agent_runtime():
    """tool_call 路由到 AgentRuntimeService.run，actor 必填"""
    ...


async def test_max_iter_terminates_loop():
    """LLM 永远返回 tool_calls → 第 3 轮后退出"""
    ...


async def test_invalid_json_arguments_returns_error_tool_message():
    """DeepSeek 偶尔吐非 JSON args → 跑 ToolResult with error"""
    ...
```

### Commit 5：chat_service.py 集成（最小改动）

**文件**：`app/services/chat_service.py`

**3 个注入点**（沿用 plan §5.5）：

```python
# 1. 在 _buildAnswerPrompt 后追加 tools_block（仅 DeepSeek 路径）
async def _callWithFallback(self, session, messages, *, model_id, actor):
    # 检测 provider 是否支持原生 tool_use
    from app.infrastructure.llm.factory import supports_native_tool_use
    
    provider = self._get_provider(model_id)  # 既有方法
    if supports_native_tool_use(provider):
        # 走 ChatMCPExecutor 循环
        executor = ChatMCPExecutor(
            llm_factory=self._llm_factory,
            adapter=self._mcp_adapter,
            runtime=self._agent_runtime_service,
            max_iter=3,
        )
        return await executor.run_loop(
            session, messages, model_id=model_id, actor=actor,
        )
    
    # 其他 provider：原 chat 流（暂不支持 MCP）
    return await self._legacy_call(messages, model_id)


# 2. lifespan 启动时调 MCPClientManager.warmup()
async def lifespan(app):
    ...
    if settings.mcp_enabled:
        await mcp_client_manager.warmup()
        mcp_adapter = MCPAdapter(mcp_client_manager)
        await mcp_adapter.hydrate_agent_registry(agent_tool_registry)
    yield
    ...
    if settings.mcp_enabled:
        await mcp_client_manager.shutdown()


# 3. SSE 流加 EVENT_MCP_TOOL_CALL 事件
async def _streamChat(self, ...):
    ...
    async for event in self._callWithFallback(...):
        if isinstance(event, dict) and "_tool_call" in event:
            yield {"event": "mcp_tool_call", "data": json.dumps(event)}
        ...
```

### Commit 6：seed + 冒烟脚本

**新文件**：`scripts/seed_mcp_agents.py`（沿用 feat-wiki-knowledge 的 `on_conflict_do_update` 模式）

```python
"""Seed MCP Agent + ACL policies.

复用 scripts/seed_agents.py 的 upsert 模式，参考 [[qa-system-seed-upsert-pattern]]。
"""
from sqlalchemy.dialects.postgresql import insert

from app.database import getSessionFactory
from app.domain.models import AgentDefinition, Policy

AGENTS = [
    {
        "code": "MCP_CONTEXT7_QUERY_DOCS_AGENT",
        "display_name": "Context7 Query Docs",
        "agent_type": "mcp",
        "tool_name": "mcp:context7:query-docs",
        "is_active": True,
    },
    {
        "code": "MCP_CONTEXT7_RESOLVE_LIB_AGENT",
        "display_name": "Context7 Resolve Library",
        "agent_type": "mcp",
        "tool_name": "mcp:context7:resolve-library-id",
        "is_active": True,
    },
]

POLICIES = [
    {"code": "MCP_CONTEXT7_QUERY_DOCS", "data_object": "MCP_CONTEXT7", "action": "allow"},
    {"code": "MCP_CONTEXT7_RESOLVE_LIB", "data_object": "MCP_CONTEXT7", "action": "allow"},
]


async def main():
    factory = getSessionFactory()
    async with factory() as session:
        for agent in AGENTS:
            stmt = insert(AgentDefinition).values(**agent)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code"],
                set_={k: stmt.excluded[k] for k in agent if k != "code"},
            )
            await session.execute(stmt)
        for policy in POLICIES:
            stmt = insert(Policy).values(**policy)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code"],
                set_={"action": stmt.excluded["action"]},
            )
            await session.execute(stmt)
        await session.commit()
    print(f"Seeded {len(AGENTS)} MCP agents and {len(POLICIES)} policies")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

**新文件**：`scripts/smoke_mcp_deepseek.sh`

```bash
#!/usr/bin/env bash
set -e

# 1. 拿 DeepSeek modelId
DEEPSEEK_ID=$(docker exec qa-postgres psql -U postgres -d qa_metadata -t -c "
  SELECT id FROM llm_config
  WHERE code ILIKE '%deepseek%' AND model_name='deepseek-chat' AND is_active=true
  LIMIT 1;
" | xargs)
echo "DeepSeek modelId: $DEEPSEEK_ID"

# 2. 登录拿 token
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"SUNql@123"}' | jq -r .accessToken)

# 3. 冒烟:让 DeepSeek 用 context7 查 FastAPI Depends
RESPONSE=$(curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"question\":\"用 context7 查一下 FastAPI 的 Depends 用法,返回简要示例\",
    \"datasourceId\":1,
    \"modelId\":$DEEPSEEK_ID,
    \"stream\":false
  }")

echo "Response: $RESPONSE" | jq

# 4. 断言
TOOL_CALLS=$(echo "$RESPONSE" | jq '.events[] | select(.event=="mcp_tool_call") | .data' 2>/dev/null | wc -l)
if [ "$TOOL_CALLS" -gt 0 ]; then
  echo "✅ MCP tool_call detected ($TOOL_CALLS calls)"
else
  echo "❌ No MCP tool_call found in response"
  exit 1
fi

# 5. 验证 mcp_call_log 有记录
docker exec qa-postgres psql -U postgres -d qa_metadata -c "
  SELECT server_name, tool_name, status, latency_ms
  FROM mcp_call_log
  ORDER BY created_at DESC LIMIT 5;
"
```

**新文件**：`backend/config/mcp_servers.yaml`

```yaml
mcp_enabled: true
max_prompt_tools: 12
tool_call_timeout_seconds: 30

servers:
  - name: context7
    transport: stdio
    command: ["npx", "-y", "@upstash/context7-mcp"]
    enabled: true
    tool_priority: high
    # api_key 不需要（Context7 无认证，任务 13 验证）
```

---

## 4. 部署与验证顺序

### 4.1 前置（每步必须通过才进入下一步）

1. **V1**：DeepSeek modelId 查到（`is_active=true` + `weight>0` + `model_name='deepseek-chat'`）
2. **V2**：API key 在容器内
3. **V3**：真机脚本 `/tmp/deepseek_tool_test.py` 跑通，`tool_calls` 不为空

### 4.2 实施顺序（TDD）

```
Commit 1: factory.py + 单测
   ↓ pytest tests/unit/test_factory_tool_use.py 通过
Commit 2: MCPClientManager + 集成测试（真 lifespan + mock subprocess）
   ↓ pytest tests/integration/test_mcp_client_manager.py 通过
Commit 3: MCPAdapter + 单测（转换 / 排序 / extractor）
   ↓ pytest tests/unit/test_mcp_adapter.py 通过
Commit 4: ChatMCPExecutor + 集成测试
   ↓ pytest tests/integration/test_chat_mcp_executor.py 通过
Commit 5: chat_service.py 接入 + lifespan
   ↓ 所有现有 chat 集成测试不回归（273 条 wiki 测试 + 原有）
Commit 6: seed + smoke 脚本
   ↓ ./scripts/smoke_mcp_deepseek.sh 通过
```

### 4.3 部署（参考 `qa-system-stale-container-deploy`）

```bash
# 改 backend 必须走 deploy_backend.sh（连 alembic/ 一起灌）
bash scripts/deploy_backend.sh

# 0084 迁移自动应用
docker exec qa-backend alembic upgrade head

# seed MCP agents
docker exec qa-backend python scripts/seed_mcp_agents.py

# 冒烟
bash scripts/smoke_mcp_deepseek.sh
```

### 4.4 灰度（按 plan §8.2）

```
Day 1: mcp_enabled=true 但 context7.enabled=false
       → 验证 lifespan warmup 路径 + AgentTool 注册数
Day 2: context7.enabled=true + 白名单 1 个 admin 用户
       → 监控 mcp_call_log 24h，看熔断统计
Day 7: 全量开启（视 Day 2-6 错误率决定）
```

---

## 5. 关键文件清单（DeepSeek-only 子集）

| 文件 | 性质 | 行数估算 |
|---|---|---|
| `app/services/mcp_client_manager.py` | 新增 | ~250 |
| `app/services/mcp_adapter.py` | 新增 | ~180 |
| `app/services/chat_mcp_executor.py` | 新增 | ~100 |
| `app/infrastructure/llm/factory.py` | 改造（+10 行） | +10 |
| `app/services/chat_service.py` | 改造（+30 行） | +30 |
| `app/main.py` + `app/tests/_testapp.py` | 改造（注册） | +10 |
| `alembic/versions/0084_mcp_call_log.py` | 新增迁移 | ~40 |
| `app/domain/mcp_models.py` | 新增 ORM | ~60 |
| `config/mcp_servers.yaml` | 新增配置 | ~20 |
| `scripts/seed_mcp_agents.py` | 新增 | ~50 |
| `scripts/smoke_mcp_deepseek.sh` | 新增 | ~40 |
| `tests/unit/test_factory_tool_use.py` | 新增 | ~30 |
| `tests/unit/test_mcp_adapter.py` | 新增 | ~80 |
| `tests/integration/test_mcp_client_manager.py` | 新增 | ~150 |
| `tests/integration/test_chat_mcp_executor.py` | 新增 | ~120 |

**总计**：~15 个文件，约 +1170 行（其中 ~380 行测试，**符合 80% 覆盖率门槛**）。

对比 plan.md 全量 1400 行（实际只实现 600 行 + 留 800 行 TODO），DeepSeek-only **砍 57% 代码 + 砍 100% XML 降级复杂度**。

---

## 6. 风险与对策（仅 DeepSeek 相关）

| 风险 | 概率 | mitigation |
|---|---|---|
| DeepSeek API 限流 / 不可达 | 中 | MCPClientManager 熔断器 + 失败降级（PlanResult.data 含 error 信息而非抛 5xx） |
| DeepSeek 偶尔吐非 JSON `arguments` | 低 | `json.JSONDecodeError` 捕获，回填 error tool message 让 LLM 重试 |
| DeepSeek **用错工具**（选 wiki_search 而非 mcp:context7:query-docs） | 中 | build_openai_tools_block 按 priority 排序 + 描述强化（plan §6 R3） |
| **Context7 silent redirect**（任务 13 重大发现） | 中 | `_sanitize_result(text)` 识别 `redirected to this library` 自动重抽新 ID 重试 |
| **Context7 业务失败 is_error=False**（任务 13） | 中 | 统一文本匹配 `not found` / `No libraries found` / `redirected to this library` 三模式 |
| 冷启动 2.2s 拖慢首问 | 中 | lazy spawn（首次 call_tool 才 connect），lifespan 只 warmup 不阻塞 |
| 0084 migration 与现有 schema drift | 低 | 沿用 audit_log / token_usage 字段风格；ORM 模型与 DDL 逐项对齐（任务 14 已完成） |

---

## 7. 不在本期范围（明确 TODO）

| 不做 | 原因 | 何时补 |
|---|---|---|
| XML 降级（Qwen/GLM/Kimi） | DeepSeek 走原生已满足需求 | 后续若用国产模型再说 |
| Streamable HTTP transport | 本期只接 Context7 stdio | 加 microsoft-learn 时 |
| LangChain `langchain-mcp-adapters` | 多一层抽象，与 plan §3"伪装 AgentTool"冲突 | 不计划引入 |
| `/admin/mcp` 管理面 UI | 配置文件管够，UI 后续补 | Phase 4 |
| 跨用户 ACL 细化 | admin 默认 allow；非 admin 默认 deny | 后续业务需求驱动 |
| SSE 流式 tool_call 事件 | 本期非流式实现 | 流式 chat 上线后补 |

---

## 8. 决策记录

| 决策点 | 选择 | 理由 |
|---|---|---|
| provider 范围 | **只支持 DeepSeek + OpenAI** | 任务 12 实测 DeepSeek 原生 100%，XML 降级仅 70% 临界 |
| transport | **stdio only**（本期） | Context7 是 npx 子进程，stdlib 即可 |
| 工具数量上限 | **12** | plan §6 R3 |
| 冷启动 | **lazy**（首次 call_tool 才 connect） | 任务 13 实测 2.2s/server |
| 错误处理 | **降级而非 5xx** | plan §6 R2，参考 wiki 工具失败降级模式 |
| ACL | **deny-by-default，actor 必填** | plan §6 R5 + ACL SSOT |
| Token 计量 | **MCP 调用不入 LLM token**（外部服务成本不在我方账上） | plan §9 关联 |
| 升级路径 | **留 XML 降级 TODO，不实现** | 节省 57% 代码 |
