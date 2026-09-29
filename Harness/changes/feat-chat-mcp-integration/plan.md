# Implementation Plan: Chat 接入 MCP 协议（feat-chat-mcp-integration）

## 1. 背景与目标

### 现状

- chat 主链路 `app/services/chat_service.py:538` 创建 `AgentRuntimeService`，`_handleAgentRun`（同文件 708 行附近）在多步 NL2SQL 后注入 L4 Agent Loop（`agent_loop.py`）。
- 已有 **Agent Tool 框架**（`app/services/agent_tools.py` + `agent_tools_wiki.py`）：`AgentToolRegistry` 注册 `name / description / data_object / input_schema / arg_extractor / handler`，4 个 wiki 工具 + 3 个内置工具（`supplier_360` / `supplier_risk` / `graph_traverse`）已上线。Handler 形如 `async (session, args, ctx) -> ToolResult`（`agent_tool_types.py:49`），返回 dataclass 含 `data/answer/tokens_used/cost/llm_model_name`。
- `BaseLlmClient.LlmMessage`（`app/infrastructure/llm/base_client.py:29,40-44`）已支持 `tool_call_id / tool_calls` 字段 → OpenAI-compatible tool API 已可在 message 层流动，但 `chat_service` 尚未在 prompt 注入工具 schema、也未在响应解析 `tool_calls`。
- LLM 路由走 `_weightedChoice` + `createClient`（`factory.py:37`），支持通义千问 / DeepSeek / GLM 等国产模型；不带 `modelId` 调 `/chat` 会 500（router 按 weight 选无 key 的 Qwen → factory None → 裸 AttributeError）。
- `Harness/mcp/README.md` 是占位目录（仅有"后续 MCP 集成占位"一段），没有运行时连接、没有契约、没有测试。

### 为什么需要 MCP

| 维度 | 现有 Agent Tool 框架 | MCP 协议接入 |
|---|---|---|
| 工具来源 | 在仓库里写 Python handler（`agent_tools.py:142`），需发版 | 任何 MCP server（stdio / SSE），外部进程 |
| 协议 | 自研 dataclass + arg_extractor 正则 | 标准化 JSON-RPC，schema 由 `list_tools` 自动发现 |
| 复用 | 每个工具一个 handler | 接入一次协议，所有 MCP server 即插即用 |
| 适配 | 新工具 = 新代码 | 第三方写好 MCP server 即可接入 |

**本质区别**：Agent Tool 框架是"工具工厂"，MCP 是"工具总线"。我们已有工厂，缺的是能挂载外部工具的总线层。MCP 让 `Context7 / microsoft-learn / 内部 ontology` 这类已经实现 MCP 协议的服务**零代码**接入 chat。

### 验收标准（可度量）

| 指标 | 目标 | 测点 |
|---|---|---|
| 接入一个新 MCP server | 配置 `mcp_servers.yaml` 一条记录，**无需改 Python 代码** | `feat-mcp-smoke` demo 脚本 |
| 工具 schema 自动发现 | `MCPAdapter` 启动时 `list_tools` 一次，缓存到 `MCPToolCache` | 单测覆盖冷/热启动 |
| chat 主链路不被 MCP 故障拖垮 | MCP server 故障时返回 503 + 友好降级，**NL2SQL 主链路仍 200** | `test_mcp_failure_does_not_block_chat` |
| 国产模型支持 | Qwen / DeepSeek / GLM 不带原生 tool-use，**降级到 prompt 注入 + JSON 解析** | 集成测试覆盖 3 个 provider |
| Token 计量 | MCP 触发的 LLM 调用走 `TokenUsageService.recordUsage`（核心约束 #2） | 冒烟脚本断言 `token_usage` 表有行 |
| ACL | MCP 工具结果不绕过 `AgentAccessPolicy` 校验（deny-by-default） | 集成测试：未授权 MCP 工具调用 → 403 |
| 灰度 | `MCP_ENABLED=false` 一行关闭 | `test_mcp_flag_off_short_circuits` |

---

## 2. 候选方案对比

| 维度 | A. MCP 协议接入 | B. 扩 Agent Tool 框架 | C. LLM 直连 Function Calling |
|---|---|---|---|
| 接入外部工具成本 | **极低**（配置即接入） | 高（每个工具一个 handler） | 中（每个工具一个 SDK 调用） |
| 协议标准化 | 行业标准（Anthropic 主导） | 自研 | 模型专属（OpenAI/Qwen 各一套） |
| 国产模型兼容 | 需 prompt 注入 + JSON 解析兜底 | 与模型无关 | **直接不兼容**（GLM/部分 Qwen 不支持） |
| 工具数量上限 | 受 prompt token 限制（需要筛选） | 受 handler 注册数限制 | 受 prompt token 限制 |
| 与现有框架关系 | **共存**：MCP 工具通过 `MCPToolAdapter` 注册为 `AgentTool`，复用 ACL/审计/计量 | 替代 | 替代 |
| 实施成本（人天） | 18-25 | 25-35 | 15-20 |
| 长期维护 | MCP 生态自带扩张 | 全自研 | 锁模型 |
| **风险** | MCP server 网络/超时、prompt 撑爆 | 工具爆炸会拖累注册表 | 模型切换即失效 |

### 推荐方案：**A. MCP 协议接入，与 Agent Tool 框架共存**

**理由**：

1. **复用而非替换**：现有 7 个 Agent Tool（4 wiki + 3 内置）已稳定运行，重写 = 把 ACL/审计/计量台账全部推倒重来。MCP 工具作为新的"工具来源"通过 `MCPAdapter.register_as_agent_tool()` 转为 `AgentTool` 实例，**直接复用** `AgentRuntimeService` 的注册 / 鉴权 / 计量链路（`agent_runtime_service.py:74`）。
2. **零代码接外部服务**：Context7 / microsoft-learn 这类第三方 MCP server 已存在，接入只需 `mcp_servers.yaml` 加一段 YAML。
3. **国产模型兼容**：国产模型（Qwen/DeepSeek）原生 tool-use 支持参差不齐，走 prompt 注入 + JSON 解析兜底是行业主流方案。
4. **协议演进**：MCP 生态在快速扩张，工具数量、协议升级由社区驱动，自研协议会落后。
5. **工具爆炸可控**：`MCPAdapter` 提供 `MAX_PROMPT_TOOLS=12` 硬上限 + `tool_priority.yaml` 人工筛选，避免 prompt 撑爆。

### 不选 B/C 的具体原因

- **不选 B**：`agent_tools.py:142` 的 handler 写死 `Supplier360Service().get360(session, key)`，对外部 MCP server 无法直接调；每个外部工具都要写 Python wrapper，与"接入零代码"目标冲突。
- **不选 C**：`model_router_service._weightedChoice` 按权重选模型，路由结果不可控；GLM-4 与 DeepSeek 的 tool-use 协议与 OpenAI 不完全兼容，直连会让国产模型被边缘化（参考 memory `qa-system-chat-llm-router-keyless-500`）。

---

## 3. 架构设计

### 整体数据流

```
                            ┌─────────────────────┐
用户问题 ──POST /chat──>    │ ChatService         │
                            │  (chat_service.py)  │
                            └──────────┬──────────┘
                                       │
                  ┌────────────────────┼────────────────────┐
                  ▼                    ▼                    ▼
           NL2SQL 引擎           L4 Agent Loop         答案生成
           (主链路)              (agent_loop.py)       (chat_stream_output.py)
                                       │
                                       │  tool_call?
                                       ▼
                            ┌─────────────────────┐
                            │ ChatMCPExecutor     │
                            │ (chat_mcp_executor) │
                            └──────────┬──────────┘
                                       │
                            ┌──────────┴──────────┐
                            ▼                     ▼
                   MCPClientManager       ToolResult → LLM
                   (mcp_client_manager)        │
                            │                ▼
                            ▼          AgentRuntimeService.run
                   MCPAdapter                  │
                   (mcp_adapter)               ▼
                            │          ACL / 审计 / Token 计量
                            ▼
                   ┌──────────────────────┐
                   │ 外部 MCP servers     │
                   │  stdio: child process│
                   │  sse:   HTTP/SSE     │
                   └──────────────────────┘
```

### 启动时序

```
lifespan startup
   │
   ├─> load mcp_servers.yaml
   │     │
   │     └─> MCPClientManager.start_all()
   │           │
   │           ├─> spawn stdio MCP server child process (asyncio.create_subprocess_exec)
   │           ├─> connect SSE MCP server (httpx.AsyncClient)
   │           └─> for each: client.list_tools() → MCPToolCache
   │
   └─> MCPAdapter.hydrate_agent_registry()
         │
         └─> for each cached tool: AgentToolRegistry.register(
                name=f"mcp:{server}:{tool}",
                data_object=f"MCP_{server.upper()}",
                input_schema=tool.inputSchema,
                arg_extractor=_MCP_ARG_EXTRACTORS[tool_name],
                handler=_MCP_DISPATCH_HANDLER,
              )
```

### chat 主链路改造点

1. **`chat_service._buildAnswerPrompt`**（位于 `chat_stream_output.py:_ANSWER_SYSTEM_PROMPT` 后）注入 `<available_tools>` 块；
2. **`chat_service._callWithFallback`** 解析响应 `tool_calls`（`LlmMessage.tool_calls` 已在 `base_client.py:44` 预留字段）；
3. **chat_mcp_executor.parse_and_dispatch** 路由到 `AgentRuntimeService.run`（注意：不绕过 ACL）。

### 与 Agent Tool 框架的边界（共存不替换）

| 范畴 | Agent Tool | MCP Tool |
|---|---|---|
| 注册源 | `AgentToolRegistry.register()`（`agent_tools.py:74`） | `MCPAdapter` 启动时批量注水 |
| 命名空间 | `supplier_360`, `wiki_search` | `mcp:context7:get-docs`, `mcp:microsoft-learn:search` |
| ACL 主题 | `data_object` 全大写 | `data_object=MCP_<SERVER>`（沿用 `_normalizeDataObject`） |
| handler 实现 | Python 同步/异步函数 | 转发到 `MCPClientManager.call_tool(server, tool, args)` |
| 失败处理 | 抛 `DomainError` 子类 | `MCPUnavailableError`(503) / `MCPAuthError`(403) / `MCPTimeoutError`(504) |
| 计量 | handler 自填 `ToolResult.tokens_used` | adapter 在 JSON-RPC 层补 token（如 server 返回 usage） |

**关键设计**：MCP 工具**不是**一个独立的执行路径，而是**伪装成 AgentTool** 注入注册表，所有调用必须经 `AgentRuntimeService.run()`（`agent_runtime_service.py:92`），强制走 ACL/状态门禁/参数抽取/计量五道闸，与自有 Agent Tool **等价**。

---

## 4. 接口契约

### 4.1 配置文件 `mcp_servers.yaml`

```yaml
# backend/config/mcp_servers.yaml
mcp_enabled: true                       # 全局开关（一键关停）
max_prompt_tools: 12                    # 注入 prompt 的工具数上限
tool_call_timeout_seconds: 30           # 单次 call 的硬超时
circuit_breaker:
  failure_threshold: 5  reset_timeout_seconds: 60

servers:
  - name: context7
    transport: stdio
    command: ["npx", "-y", "@upstash/context7-mcp"]
    env: { CONTEXT7_API_KEY: "${env:CONTEXT7_API_KEY}" }
    enabled: true
    tool_priority: high

  - name: microsoft-learn
    transport: sse
    url: https://learn.microsoft.com/api/mcp
    enabled: true
    tool_priority: normal

  - name: internal-ontology
    transport: stdio
    command: ["python", "-m", "mcp_servers.ontology_server"]
    enabled: false                      # 灰度：先关
    tool_priority: low
```

**契约要点**：`${env:XXX}` 占位符在 `MCPSettings.__post_init__` 解析为 `os.environ["XXX"]`；`enabled=false` 的 server **不** spawn、不 `list_tools`；`tool_priority` 决定 prompt 注入顺序；与 `system_config` 表同级。

### 4.2 LLM 端 prompt 注入格式

#### A. 走原生 tool_use 的 provider（OpenAI / DeepSeek 兼容模式）

```json
{
  "tools": [{
    "type": "function",
    "function": {
      "name": "mcp:context7:get-docs",
      "description": "从 Context7 拉取第三方库最新文档",
      "parameters": {"type": "object", "properties": {...}}
    }
  }]
}
```

#### B. 国产模型降级（Qwen / GLM / 无 tool-use）

```
<available_tools>
1. mcp:context7:get-docs — 从 Context7 拉取第三方库最新文档
   参数: library (string, 必填); topic (string, 可选)
2. mcp:microsoft-learn:search — 在 Microsoft 官方文档搜索
   参数: query (string, 必填); top_k (int, 可选, 默认 10)
</available_tools>

调用规则：需要工具时，**只输出**以下 JSON（不要其他文字）：
{"tool_call": {"name": "<工具名>", "arguments": {<参数>}}}
无工具调用时，正常回答。
```

**Prompt 注入边界**：`<available_tools>` 块包裹在 prompt fence 内（复用 `app/services/learning/prompt_fence.py:neutralizeFence`，参考 memory `qa-system-prompt-fence-neutralize` M4 的 HIGH 教训）。

### 4.3 新增 API（管理面）

```
GET    /api/v1/mcp/servers                      列出所有 MCP server
POST   /api/v1/mcp/servers/{name}/restart       重连单个 server
GET    /api/v1/mcp/tools                        列出所有发现的工具
PATCH  /api/v1/mcp/config                       更新 mcp_enabled
```

### 4.4 前端契约（管理页 1 条）

`AdminMCPPage.vue` 挂在 `/admin/mcp`，i18n 命名空间 `menu.item.adminMcp` + `mcp` 顶层块；与 `scripts/seed_menu_config.py`同步加项。

---

## 5. 实现要点

### 5.1 `app/services/mcp_client_manager.py`（**新文件**，~250 行）

**职责**：MCP server 连接池 + 生命周期 + 熔断。

```python
class MCPClientManager:
    def __init__(self, settings: MCPSettings):
        self._clients: dict[str, MCPClient] = {}
        self._breaker: dict[str, CircuitBreaker] = {}

    async def start_all(self) -> None:
        for cfg in self._settings.servers:
            if not cfg.enabled: continue
            client = await self._connect(cfg) # stdio 或 sse
            tools = await client.list_tools()
            self._clients[cfg.name] = client
            self._tool_cache[cfg.name] = tools

    async def call_tool(self, server, tool, args, *, timeout):
        if server not in self._clients:
            raise MCPUnavailableError(...)
        breaker = self._breaker.setdefault(server, CircuitBreaker(...))
        if breaker.is_open(): raise MCPUnavailableError(...)
        try:
            return await asyncio.wait_for(
                self._clients[server].call_tool(tool, args), timeout=timeout)
        except (TimeoutError, ConnectionError) as exc:
            breaker.record_failure(); raise MCPTimeoutError(...) from exc

    async def stop_all(self) -> None:
        for c in self._clients.values(): await c.close()
```

**关键点**：`stdio` 用 `asyncio.create_subprocess_exec`（参考 `ollama_client.py`）；`sse` 用 `httpx.AsyncClient` 长连接；熔断器用简单滑动窗口（不引入新依赖）；启动失败 `logger.warning` + 标 degraded，不阻塞 lifespan。

### 5.2 `app/services/mcp_adapter.py`（**新文件**，~180 行）

**职责**：list_tools → 工具 schema → 转为 AgentTool + prompt 注入。

```python
class MCPAdapter:
    async def hydrate_agent_registry(self, registry: AgentToolRegistry) -> None:
        for server_name, tools in self._mgr.iter_tools():
            for tool in tools:
                registry.register(AgentTool(
                    name=f"mcp:{server_name}:{tool.name}",
                    description=tool.description,
                    data_object=f"MCP_{server_name.upper()}",
                    data_layers=(),
                    input_schema=tool.inputSchema,
                    arg_extractor=self._build_arg_extractor(tool),
                    handler=_make_dispatch_handler(server_name, tool.name),
                ))

    def build_prompt_block(self, *, max_tools, provider) -> str | dict | None:
        tools = self._rank_tools()[:max_tools]
        if provider in _NATIVE_TOOL_USE_PROVIDERS:
            return self._build_native_tools(tools)
        return self._build_xml_block(tools)
```

**关键点**：工具排名 = `tool_priority` + 上次调用成功率 + 时间衰减；注入时记录 `meta.injected_tools`便于审计；`_make_dispatch_handler` 是闭包工厂。

### 5.3 `app/services/chat_mcp_executor.py`（**新文件**，~150 行）

**职责**：解析 LLM `tool_calls` + 调用 + 结果回填。

```python
class ChatMCPExecutor:
    async def run_loop(self, session, messages, *, llm_factory, actor):
        appended: list[LlmMessage] = []
        for i in range(self._max_iter):
            resp = await self._llm_call(messages + appended, llm_factory)
            if not resp.tool_calls:
                appended.append(LlmMessage(role="assistant", content=resp.content))
                break
            appended.append(LlmMessage(role="assistant", content=resp.content,
                                       tool_calls=resp.tool_calls))
            for tc in resp.tool_calls:
                agent_code = _tool_name_to_agent_code(tc.function.name)
                try:
                    result = await self._runtime.run(
                        session, agent_code, json.loads(tc.function.arguments),
                        llm_factory=llm_factory, actor=actor)
                    content = json.dumps(result.data, ensure_ascii=False)
                except DomainError as exc:
                    content = json.dumps({"error": str(exc)}, ensure_ascii=False)
                appended.append(LlmMessage(role="tool", tool_call_id=tc.id, content=content))
        return appended
```

**关键点**：`max_iter=3` 与 L4 iteration budget 一致；异常捕获 `DomainError` 不吞系统异常；`_tool_name_to_agent_code` 把 `mcp:context7:get-docs` → `MCP_CONTEXT7_GET_DOCS_AGENT`（与 §10B.1 同源）。

### 5.4 `app/infrastructure/llm/factory.py` 改造（**最小改动**，+30 行）

仅增加 provider 是否支持原生 tool-use 的标记：

```python
_NATIVE_TOOL_USE_PROVIDERS = frozenset({
    ProviderType.OPENAI, ProviderType.AZURE_OPENAI,
    ProviderType.OPENAI_COMPATIBLE_PROXY, # DeepSeek 等
})
# Qwen / GLM / Ollama 不在内 → 走 prompt 注入 + JSON 解析

def supports_native_tool_use(provider: ProviderType) -> bool:
    return provider in _NATIVE_TOOL_USE_PROVIDERS
```

**理由**：不改 LLM 客户端本身，仅在工厂层暴露查询接口，让 `MCPAdapter.build_prompt_block` 按 provider 走对应分支。

### 5.5 `app/services/chat_service.py` 改造（**最小改动**，~50 行新增）

3 个注入点：1) `_buildAnswerPrompt` 后追加 `available_tools` 块；2) `_callWithFallback` 末尾检测响应 `tool_calls` 并循环；3) SSE 流加 `EVENT_MCP_TOOL_CALL` 事件。

### 5.6 `Harness/changes/feat-chat-mcp-integration/summary.md`

按 `_template/summary.md` 格式记录 SSOT：需求 / 决策 / 已验证代码事实 / 实施步骤 / 关键坑 / 验收指标。每个里程碑（Phase 1 启动连接 / Phase 2 AgentTool 注入 / Phase 3 chat 主链路接入）独立 commit。

---

## 6. 风险与对策

### R1. 国产 LLM 无 function calling 协议支持

**mitigation**：响应解析用**严格**正则 `re.fullmatch(r'\{...\}', ...)`，失败直接当"无 tool_call"继续；`_validate_tool_call_args(tc, schema)` 用 jsonschema 校验；解析失败落 `mcp_call_log` 记原始输出便于排查；`max_iter=3` 兜底。

### R2. MCP server 故障拖垮 chat 体验

**mitigation**：熔断器（连续5 次失败开 60s）+ 单点隔离（每个 server 独立连接/进程）+ 总闸门 `mcp_enabled=false` 立即跳过 + `ChatMCPExecutor` 捕获 `MCPUnavailableError` 后降级而非抛 5xx（参考 `agent_tools_wiki._wikiSearchHandler` 语义检索失败降级关键词的模式）。

### R3. 工具数量爆炸撑爆 prompt

**mitigation**：`max_prompt_tools=12` 硬上限；工具排名 `_rank_tools`（`tool_priority` + 7 天成功率 + 时间衰减）；超限工具在 prompt 里只显示 `name` 一句话 description，schema 仅 high priority 完整注入；`meta.injected_tools_count` 与 `total_tools_count` 落 `token_usage` 便于观察。

### R4. Docker 容器 → 宿主机 MCP server 的网络穿透

**mitigation**：`docker-compose.yml` 加 `extra_hosts: ["host.docker.internal:host-gateway"]`；stdio 模式用容器内绝对路径；sse 模式用 `http://host.docker.internal:<port>/sse`（开发）；`MCPSettings.__post_init__` 校验可达性，lifespan 失败 `SystemExit(1)`；`scripts/smoke_mcp_chat.sh` 用 `docker exec` 验证连通性（参考 memory `qa-system-frontend-deploy-build-required` 同源陷阱）。

### R5. 工具调用结果未做权限校验（SSRF / 信息泄露）

**mitigation**：`MCPAdapter.sanitize_result` 用正则剥 `<internal_url>` / `<secret>` 敏感片段；`ChatMCPExecutor` 调 `AgentRuntimeService.run` 时 `actor` 必填走 `_enforcePolicies` deny-by-default；新建 `mcp_call_log` 表（migration 0063）记录 `actor/server/tool/args/result_size/status`，`purge_after_days=30`；token 注入检测：响应含 `sk-` / `Bearer ` 正则报警并丢弃；`allowed_servers` 白名单启动校验防配置漂移。

---

## 7. 测试策略

### 7.1 单元测试（`app/tests/unit/`）

| 文件 | 覆盖 |
|---|---|
| `test_mcp_client_manager.py` | 连接池 / 熔断 / 超时 / 重连；mock subprocess + httpx |
| `test_mcp_adapter.py` | `build_prompt_block` 按 provider 分支；`hydrate_agent_registry` mock |
| `test_chat_mcp_executor.py` | 解析 `tool_calls` + 错误回填 + max_iter 截断 |
| `test_prompt_fence_neutralize.py` | `<available_tools>` 围栏隔离（继承 M4 教训） |

**关键断言**：熔断器第 5 次失败后第 6 次直接 raise 不调真实连接（`AsyncMock.assert_not_called`）；prompt 注入 `meta.injected_tools_count == min(total_tools, max_prompt_tools)`；国产降级路径产出 XML 块、原生 tool_use 路径产出 JSON tools array。

### 7.2 集成测试（`app/tests/integration/`，**真 PG + 真 lifespan**）

| 测试 | 链路 |
|---|---|
| `test_mcp_lifespan_connects_to_stdio_server` | 启动 mock MCP server（subprocess），断言 `MCPToolCache` 有工具 |
| `test_mcp_lifespan_handles_server_failure_gracefully` | server 启动失败 → lifespan 不抛、该 server 标 degraded |
| `test_mcp_tool_appears_in_agent_registry_after_startup` | `MCPAdapter.hydrate_agent_registry` → `AgentToolRegistry.has(name)` |
| `test_mcp_tool_call_runs_through_agent_runtime_with_acl` | `AgentRuntimeService.run(MCP_*)` 走完整 ACL/参数抽取/handler |
| `test_mcp_tool_call_without_acl_returns_403` | 删策略 → 调用 → 403 |
| `test_mcp_failure_does_not_block_chat_main_path` | mock MCP 抛 `MCPUnavailableError` → `/chat` 仍 200 |
| `test_mcp_iteration_loop_terminates_at_max_iter` | mock LLM 永远返回 `tool_calls` → 3 轮后退出 |

### 7.3 阻塞 / 隔离策略

- **`MCP_ENABLED` 默认 `false`**，需显式 `--feature mcp-on` 启动才注入（与 `WIKI_VECTOR_SYNC_ENABLED` 同源教训）；
- 集成测试用 `monkeypatch` 临时开启，全局关闭不影响其他 273 条 wiki 集成测试；
- `tests/conftest.py` 提供 `make_fake_mcp_server()` 工具。

### 7.4 真机冒烟脚本（`scripts/smoke_mcp_chat.sh`）

```bash
#!/usr/bin/env bash
set -e
docker compose up -d qa-backend
docker exec qa-backend python -c "
from app.services.mcp_client_manager import MCPClientManager
from app.config import getSettings
import asyncio
async def main():
    mgr = MCPClientManager(getSettings().mcp)
    await mgr.start_all()
    print('tools:', await mgr.list_all_tool_names())
asyncio.run(main())
"
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "用 context7 查 fastapi 的 Depends 用法", "modelId": 1, "stream": false}'
# 断言响应 SSE 含 EVENT_MCP_TOOL_CALL
docker compose down
```

---

## 8. 部署与回滚

### 8.1 部署脚本（`scripts/deploy_mcp_chat.sh`）

1. `alembic upgrade head` 应用 0063 迁移
2. `docker compose build --no-cache backend`
3. `docker compose up -d backend`
4. `./scripts/smoke_mcp_chat.sh` 真机冒烟
5. 监控 `token_usage` / `mcp_call_log` 24h

**注意**：参考 memory `qa-system-stale-container-deploy` —— 改 backend 必走 `./scripts/deploy_backend.sh`（连 alembic/ 一起灌）。

### 8.2 灰度方案

| 维度 | 灰度方式 | 配置位置 |
|---|---|---|
| 全局开关 | `mcp_enabled: true/false` | `mcp_servers.yaml` |
| 单 server | `enabled: true/false` | 同上 |
| 单模型 | provider 维度：`supported_providers: [OPENAI, DEEPSEEK]` | `MCPAdapter` |
| 单用户 | `mcp_user_whitelist: list[str]` | `MCPSettings` + `user_config` |

**推荐顺序**：先开 `mcp_enabled=true` 但所有 server `enabled=false`（验证 prompt 注入路径）→ 开 `context7` + `modelId=1` 灰度 1 个用户 → 7 天无异常 → 全量开启。

### 8.3 回滚策略

- **配置回滚（一键关）**：`mcp_enabled=false` → `MCPClientManager.start_all` 立即跳过，`/chat` 路径回到 MCP 接入前。下一次 lifespan 重启或热重载（`POST /api/v1/mcp/config` PATCH 后触发 `MCPSettings.reload()`）生效。
- **代码回滚**：标准 `git revert <merge-commit>`；alembic `downgrade -1` 撤 0063 迁移。
- **数据回滚**：`mcp_call_log` 表是新建的，drop 即可，不影响业务表。

---

## 9. 关联

### 与既有特性的关系

| 关联 | 说明 |
|---|---|
| **feat-wiki-knowledge** | M8 落地的 4 个 Agent Tool 是 **MCP 集成的模板**：`AgentToolRegistry` 注册模式、ACL/计量链路、`ChatAgentLoop` 集成方式全部可复用。MCP 工具通过 `MCPAdapter.hydrate_agent_registry` 走**同一条** `AgentTool` 注册链路。 |
| **L4 iteration budget**（memory `qa-system-l4-iteration-budget`） | `ChatMCPExecutor.max_iter=3` 与 L4 `max_iterations=5` **共享同一个死循环防御语义**；不引入新 budget 机制以免配置矩阵膨胀。 |
| **Token 计量** | MCP 触发的 LLM 调用必须经 `TokenUsageService.recordUsage`；MCP 工具自身的 server-side token 不计入（外部服务成本不在我方账上）。`mcp_call_log` 单独跟踪 MCP 调用次数/失败率，与 LLM token 用量解耦。 |
| **Prompt 围栏隔离**（memory `qa-system-prompt-fence-neutralize`） | `<available_tools>` 块沿用 `app/services/learning/prompt_fence.py:neutralizeFence`（M4/M5 共用 SSOT），**不**重写一份；并写变异验证。 |
| **Agent ACL**（memory `acl-security-review-pattern`） | MCP 工具 `data_object=MCP_<SERVER>` 走既有 `_normalizeDataObject`（`agent_tools.py:89`），复用 ACL 4 项检查。 |
| **Wiki 词表 SSOT**（memory `qa-system-agent-vocabulary`） | MCP server name 走 `MCP_SERVERS` 常量词表，避免硬编码字符串漂移。 |

### 跨文件链接（**至少 3 条**）

1. [feat-wiki-knowledge §10B.1](../feat-wiki-knowledge/summary.md#10b1-一个-agent-只能绑一个工具--4-个知识工具--4-个-agent) —— 一个 Agent = 一个工具的命名约定，MCP 复用同模式。
2. [feat-wiki-knowledge §10B.5](../feat-wiki-knowledge/summary.md#10b5-策略种子必须跟着工具走) —— `_policiesFor` 的"策略跟着工具走"模式直接套用于 MCP server 启停后的策略愈合。
3. [architecture.md §核心组件](../../wiki/architecture.md#核心组件) —— `MCPClientManager / MCPAdapter / ChatMCPExecutor` 三个新组件补到组件清单。
4. [model-router.md §模型降级重试](../../wiki/model-router.md#模型降级重试54) —— 国产模型不支持原生 tool-use 时 `_callWithFallback` 的降级语义。
5. [testing.md 规范](../../../../../../../../.claude/rules/common/testing.md) —— TDD RED-GREEN-IMPROVE 与 80% 覆盖率门槛；`tests/integration/test_mcp_*.py` 必须走真实 PG（参考 memory `qa-system-runbook-quirks`）。

### 进度跟踪

| Phase | 内容 | 状态 | 验证 |
|---|---|---|---|
| Phase 1 | `MCPClientManager` + lifespan 接入 + stdio 模式连通 | ⏳ 待开始 | 单测 + 真机冒烟 |
| Phase 2 | `MCPAdapter.hydrate_agent_registry` + ACL seed | ⏳ 待开始 | 集成测试 + 文档 |
| Phase 3 | `ChatMCPExecutor` + chat 主链路改造 + 国产模型降级 | ⏳ 待开始 | 端到端 demo |
| Phase 4 | 管理面 `/admin/mcp` + 灰度 / 熔断 / 审计 | ⏳ 待开始 | 真机冒烟 + 灰度方案评审 |

### 前置调研（强烈建议先做）

1. **MCP 协议现状**（半天）：调研官方 `modelcontextprotocol/python-sdk` 当前支持的 transport（stdio / SSE / Streamable HTTP 最新进展），决定 sse 模式用哪个 client；
2. **国产模型 tool-use 实测**（半天）：在 qwen / deepseek / glm 各跑 10 条 prompt，统计 XML 块解析成功率，**这是推荐方案成立的关键前提**；成功率 < 70% 则必须切到原生 tool-use，并接受国产模型不可用；
3. **Context7 MCP server 实地连一次**（2 小时）：在本地起一个 context7-mcp，走 `list_tools` → `call_tool` → 看响应 schema 是否稳定（`Harness/mcp/README.md` 已点名此 server）。

---

## 附录 A：关键文件清单

| 文件 | 性质 | 行数估算 |
|---|---|---|
| `app/services/mcp_client_manager.py` | 新增 | ~250 |
| `app/services/mcp_adapter.py` | 新增 | ~180 |
| `app/services/chat_mcp_executor.py` | 新增 | ~150 |
| `app/services/mcp_settings.py` | 新增 | ~120 |
| `app/api/v1/mcp.py` | 新增 | ~100 |
| `app/domain/mcp_models.py` | 新增 | ~60 |
| `app/infrastructure/llm/factory.py` | 改造（+30 行） | +30 |
| `app/services/chat_service.py` | 改造（+50 行） | +50 |
| `app/main.py` + `app/tests/_testapp.py` | 改造（注册 router） | +10 |
| `alembic/versions/0063_mcp_call_log.py` | 新增迁移 | ~40 |
| `config/mcp_servers.yaml` | 新增配置 | ~50 |
| `frontend/src/pages/AdminMCPPage.tsx` | 新增管理页 | ~200 |
| `scripts/seed_menu_config.py` | 改造（+5 行） | +5 |
| `Harness/changes/feat-chat-mcp-integration/summary.md` | 新增 SSOT | ~250 |
| `Harness/wiki/architecture.md` | 改造（+30 行） | +30 |

**总计**：~13 个文件，约 +1400 行（其中 ~700 行测试，符合 80% 覆盖率门槛）。
