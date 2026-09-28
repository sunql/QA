# MCP Python SDK 调研(2026-09-22 时点)

> 给 `app/services/mcp_client_manager.py` 设计做前置依据。SSOT 镜像源: `modelcontextprotocol/python-sdk` (24.3k stars, last push 2026-09-19),PyPI `mcp` 包。

---

## 1. 现状摘要(一句话先行)

MCP 协议在 **2025-03-26 spec 修订**时把传输层正式收敛为两套:`stdio`(子进程 JSON-RPC)+ `Streamable HTTP`(单端点 HTTP+SSE 双向流)。**老的 HTTP+SSE 双端点传输(`sse_client` 用法)已被官方标 deprecated,在 2.x 主线进入"兼容但不再开发"状态**。Python SDK 走双轨发布:`v1.30.0`(2026-09-07)与 `v2.2.0`(2026-09-07)并行,**2.0 引入了破坏性字段重命名**(`inputSchema`→`input_schema`, `isError`→`is_error`, 返回元组从 3 元变 2 元等),生态 84% 包未锁上界,autogen-ext 等依赖 v1.x 真有 pull-through 踩坑报告。

我们 `plan.md` 第 4.1 / §5 写的是 `tool.inputSchema` / `result.isError`(v1 camelCase),**这强烈暗示我们要锁 v1 主线**,而不是追新。具体建议见文末。

---

## 2. Transport stable 状态(截至 2026-09-22)

| Transport | 当前状态 | 适用场景 | 备注 |
|---|---|---|---|
| **stdio**(子进程 stdin/stdout JSON-RPC) | **Stable**(主推) | 本地 MCP server(`npx -y @upstash/context7-mcp` 这类),延迟最低、零网络 | 我们 `mcp_servers.yaml` 主用例 |
| **Streamable HTTP** | **Stable**(2025-03-26 起主推) | 远端 MCP server(serverless 友好、支持 resumability) | 单端点,支持 POST 请求体 + 同端点 SSE 上行 |
| **SSE**(老 HTTP+SSE 双端点,GET 拉 + POST 推) | **Deprecated**(2.x 兼容保留,1.x 仍可但官方不再开发) | 仅迁移期遗留 | 我们的新代码**不**应再写 `sse_client` |
| WebSocket | Stable(第三方/server 端可选) | 自定义部署 | 不在官方 spec 必选,不推荐依赖 |

要点:
- 旧 SSE 传输在 spec 2024-11-05 时定义,2025-03-26 spec 修订时被 Streamable HTTP 取代。
- Streamable HTTP 不是"去掉 SSE",而是**统一到一个 HTTP 端点,内部按需用 SSE 流式**;server 仍可返回 `text/event-stream`,但 client 只看到单端点。

---

## 3. 推荐 Client 入口

### 3.1 异步 transport 函数

| 函数 | 来源 | 用法 |
|---|---|---|
| `stdio_client(server_params)` | `mcp.client.stdio` | 返回 `(read, write)` 二元组,`async with` |
| `streamablehttp_client(url, ...)` | `mcp.client.streamable_http`(v1) / `mcp.client.streamable_http.streamable_http_client`(v2) | 返回 `(read, write)`(v1 还多一个 `get_session_id_callback`) |
| ~~`sse_client`~~ | `mcp.client.sse` | **不推荐新代码用** |

### 3.2 Session

- **`ClientSession`**(`mcp.ClientSession`,不是 `MCPClient` 之类)是唯一官方推荐的 session 封装,无新封装。
- 构造:`ClientSession(read, write)` → 必须先 `async with`,内部 `await session.initialize()` 完成 MCP 握手。
- **cleanup 用 `async with`**,不要手写 `await session.close()`,context manager 会一并关掉 transport 的 task / 子进程 / SSE 流。

### 3.3 标准范式(我们 plan 要写的)

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(command="npx", args=["-y", "@upstash/context7-mcp"], env={...})
async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("get-docs", arguments={"library": "fastapi"})
```

Streamable HTTP 唯一区别是把 `stdio_client(params)` 换成 `streamablehttp_client(url)`,session/上下文用法完全一致。

---

## 4. `list_tools` / `call_tool` 契约

| 调用 | 返回类型 | 关键字段 |
|---|---|---|
| `await session.list_tools()` | `ListToolsResult` | `.tools: list[Tool]`、`.next_cursor: str | None` |
| `Tool` 对象(v1 camelCase) | — | `.name`、`description`、`inputSchema: dict` |
| `await session.call_tool(name, arguments={...})` | `CallToolResult` | `.content: list[TextContent|ImageContent|EmbeddedResource]`、`.isError: bool`(v1) |
| v2 字段重命名 | — | `inputSchema`→`input_schema`、`isError`→`is_error`、新增 `structured_content`、`_meta` |

### 4.1 错误处理(必须分清三层)

- **协议层错误**(JSON-RPC 解析失败、方法不存在):SDK 抛 **`McpError`**(v1)/ `MCPError`(v2),带 `.code` / `.message`。
- **工具层错误**(工具被调用但执行失败):`CallToolResult.is_error=True`,**不抛异常**,需要客户端主动检查 `if result.is_error:`(v1)/ `is_error`(v2)。这是 MCP spec 设计:让模型能在 tool message 里自纠正。
- **Transport 层错误**(子进程崩、连接超时):SDK 抛 `ConnectionError` / `TimeoutError` / `anyio` 系列;`streamablehttp_client` 会通过 session 异常带出 HTTP 状态码。

`MCPClientManager` 必须三处都拦:不要只 `try/except McpError`,`is_error=True` 要走专门分支(对应 plan §5 提的 `MCPUnavailableError` / `MCPAuthError` / `MCPTimeoutError`)。

### 4.2 args 类型

- 官方契约:**`dict[str, Any]`**(keyword arg,默认 `{}`),不是 Pydantic。
- server 端 v1 自动用 `inputSchema` 校验 args;**v2 移除了自动 JSON Schema 校验**,必须 server 自己手验。这影响我们写 MCP server 时(如未来要做 in-house MCP),但 client 不受影响。
- 我们的 `MCPAdapter` 注入 AgentTool 时拿 `tool.inputSchema`(v1)作为 `input_schema`,handler 把 dict 透传给 `call_tool` 即可。

---

## 5. 版本与依赖

### 5.1 版本号(2026-09-22 时点)

| 包 | 最新稳定 | 发布日期 |
|---|---|---|
| `mcp`(v1 主线,**锁这条**) | **1.30.0** | 2026-09-07 |
| `mcp`(v2 主线,有破坏性) | 2.2.0 | 2026-09-07 |
| Python | requires-python=">=3.10";classifier 含 3.10/3.11/3.12/3.13/3.14 | — |

我们 Docker 镜像跑的是 Python 3.14,**完全支持**,无需降级。

### 5.2 依赖审计(实测 pyproject.toml)

`mcp` 自身的运行时依赖是动态声明(dynamic=["version","dependencies"]),核心 deps 实质为:
- `pydantic`(数据契约,跟 LLM 工具 schema 一脉相承)
- `anyio`(async 抽象层,stdlib asyncio 也兼容)
- `httpx`(HTTP client,Streamable HTTP 走它)
- `httpx-sse`(SSE 上行流解析)
- `jsonschema`(draft-07/draft-2020-12 校验)
- `starlette` + `uvicorn`(server 端,client 用不到)
- `typing-extensions`

**结论:无 torch / numpy / langchain-core / transformers 重负担。** 镜像层增量预计 30~50MB,跟我们现有 `fastapi`/`sqlalchemy` 栈完全兼容。v1 与 v2 依赖基本一致。

### 5.3 v1 vs v2 选择:锁 v1

| 维度 | v1.30.0 | v2.2.0 |
|---|---|---|
| 字段命名 | camelCase(`inputSchema`, `isError`) | snake_case(`input_schema`, `is_error`) |
| 生态兼容 | 100%(autogen-ext、llama-index-tools-mcp 等未升) | 生态正在修复,autogen-ext 0.7.5 显式 broken |
| `streamablehttp_client` 返回 | 3 元 `(read, write, get_session_id)` | 2 元 `(read, write)` |
| spec 协议版本 | 2025-03-26 | 2025-11-25(增 `structured_content` / `_meta`) |
| `FastMCP` 类名 | `FastMCP` | `MCPServer` |
| 错误类名 | `McpError` | `MCPError` |

**Plan §5 / §6 的代码片段写的是 camelCase**(`tool.inputSchema` / `result.isError`),硬要上 v2 会大面积改名 + 改 `streamable_http_client` 拼包名 + 改 `MCPError` 类名 + 改 `MCPServer`,工作量翻倍且无功能收益。**结论:锁 v1.30.0**,等生态全部 ship v2 一致后再做一次 planner migration)。

---

## 6. 上层封装(可选,简要)

| 包 | 定位 | 是否值得用 |
|---|---|---|
| `langchain-mcp-adapters`(`MultiServerMCPClient`) | 把多 MCP server 的工具转成 LangChain `BaseTool`,4 层抽象(transport→session→adapter→LC tool) | **不值得**。它专为 LangGraph/`create_agent` 服务,而我们 chat 是自建 `AgentRuntimeService`,直接走 `ClientSession` 反而少一层依赖、还能精确控制 ACL 注入 |
| `mcp-client-cli` 之类 helper | 命令行调试用 | 不进运行时,只用于本地烟测 |

**判断:`MCPClientManager` 直接调 `mcp.client.stdio` / `mcp.client.streamable_http` + `mcp.ClientSession`,不引 `langchain-mcp-adapters`。** 否则 ACL、token 计量、cache 都要穿透一层间接层,与 plan §3 "MCP 工具伪装成 AgentTool 注入注册表"的设计冲突。

---

## 7. 行动建议(给 plan §5.1 `MCPClientManager`)

### 7.1 pip 锁定

```toml
# pyproject.toml 后端依赖节
mcp = ">=1.30,<2"          # 锁 v1 主线,等生态全 v2 后再迁
# 显式锁上界防 pull-through 踩坑(autogen-ext / llama-index 教训)
```

注意:**只锁 `mcp`,不要拉 `langchain-mcp-adapters`**(理由见 §6)。

### 7.2 transport 选择

- **首选 stdio**:本机 npx / uvx 起的 MCP server(plan §4.1 已写 `npx -y @upstash/context7-mcp`),延迟最低、不占端口、ACL 边界最清晰(子进程死了立即报错)。
- **次选 Streamable HTTP**:远端 / SaaS MCP server(如未来对接 context7 官方远端)。实现范式与 stdio 仅 transport 函数一行之差。
- **不用 SSE**(`sse_client`):已 deprecated,新代码不写。

### 7.3 Client 入口与生命周期

```python
async with streamablehttp_client(url) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        # ...
# 或 stdio:
async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
            await session.initialize()
```

- 不写 `MCPClient` 自封装,直接用官方 `ClientSession`。
- **必须 `async with`**,不要 `try/finally + close`,后者会漏掉 transport 的 task 取消。
- lifespan 启动时一次性 `list_tools` 灌进 `MCPToolCache`(plan §5 已有);运行时 `call_tool` 不要重复 `list_tools`。
- `tool.inputSchema`(v1 camelCase)直接给 `AgentToolRegistry.register(input_schema=...)`,handler 转发 dict 给 `call_tool`。

### 7.4 错误分层(在 `MCPClientManager.call_tool` 内)

```python
try:
    result = await session.call_tool(name, arguments=args)
except McpError as e:                       # JSON-RPC / 方法不存在
    raise MCPUnavailableError(503) from e
except (TimeoutError, ConnectionError):     # 子进程崩 / 网络断
    raise MCPTimeoutError(504)
if result.isError:                          # 工具执行失败,但协议通
    raise MCPAuthError(403) if "auth" in str(result.content) else MCPUnavailableError(503)
```

### 7.5 前置兼容性验证(必须做)

1. **锁版本烟测**:在 feat-chat-mcp-integration 分支装 `mcp==1.30.0`,把 `examples/snippets/clients/streamable_basic.py` 跑通,作为回归基线。
2. **写一个 in-house mock MCP server**(pytest fixture,stdio 模式)跑 E2E:`start_all → list_tools 缓存 → AgentRuntimeService.run → tool_call → ACL 拦截 → 错误返回 isError`,这是 §5 集成测试的最小骨架。
3. **跑一次 pull-through 验证**:`pip install mcp==1.30.0` 后 `pip check` + `pip list | grep -i 'langchain\|autogen\|llama-index'`,确认没被上层包跨主版本拉上 v2;若被拉,加 `pip install mcp==1.30.0` 二次锁。
4. **不要立刻追 2.x**:等 `autogen-ext` / `llama-index-tools-mcp` 全部发 v2 兼容版(目前已知 `llama-index-tools-mcp>=0.5.1` 已修),再开迁移 PR。

---

## 来源

- [modelcontextprotocol/python-sdk GitHub](https://github.com/modelcontextprotocol/python-sdk)(releases via `gh api`,2026-09-07 v1.30.0 / v2.2.0)
- [PyPI mcp 历史](https://pypi.org/project/mcp/#history)
- [MCP Transports Spec 2025-03-26](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
- [MCP Python SDK v1→v2 Migration Guide](https://py.sdk.modelcontextprotocol.io/zh/migration/)
- [The MCP Python SDK 2.0 broke the wrapper libraries (dev.to)](https://dev.to/milkyway008/the-mcp-python-sdk-20-broke-the-wrapper-libraries-heres-how-to-spot-it-and-pin-back-346n)
- [MCP Goes Stateless: 2026-07-28 spec rewrite (niteagent)](https://niteagent.com/blog/mcp-stateless-2026-spec-deep-dive)
- [langchain-mcp-adapters Reference](https://reference.langchain.com/python/langchain_mcp_adapters/)
- [langchain-mcp-adapters Session Management (deepwiki)](https://deepwiki.com/langchain-ai/langchain-mcp-adapters/3-session-management-and-transport)