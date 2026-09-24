---
created: 2026-09-22
sources:
  - https://github.com/upstash/context7
  - https://www.npmjs.com/package/@upstash/context7-mcp
tags: [mcp, context7, plan-§5.1, plan-§5.2, end-to-end]
---

# Context7 MCP Server 实测报告

## 1. 启动可行性

- **包**：`@upstash/context7-mcp@4.1.1`（npm latest，2026-09 当前版本）
- **启动命令**：`npx -y @upstash/context7-mcp`，纯 stdio 模式，无任何 CLI 参数。
- **首次启动**：本地首次 `npx` 下载 ~174 KB tarball，stderr 输出
  `Context7 Documentation MCP Server v4.1.1 running on stdio`，然后阻塞等待 JSON-RPC stdin。
- **Node 要求**：Node 18+（实测 Node v26.7.0 通过）。
- **Python 客户端**：`mcp` Python 包（实测 1.x 已装在 `python3.14` site-packages）。
- **API key**：**不需要**。冷启动无 env var、无 prompt，直接进入工作状态（1.85s 完成首个 `resolve-library-id`）。
- **协议版本**：服务端返回 `protocol_version="2025-11-25"`（注意是 2025-11-25，不是规范的 2024-11-05；`mcp` Python 客户端兼容）。

## 2. `list_tools` schema 稳定性

5 次冷启动连续调用 `tools/list`，schema **完全一致**（baseline 5/5 命中）：

| 字段 | resolve-library-id | query-docs |
|---|---|---|
| `required` | `[libraryName, query]` | `[libraryId, query]` |
| `properties` | `{libraryName, query}` | `{libraryId, query}` |
| `annotations` | `read_only=True, destructive=False, idempotent=True, open_world=True` | 同上 |
| `$schema` | `https://json-schema.org/draft/2020-12/schema` | 同上 |

每个 tool 对象还含 `name` / `title` / `description` / `input_schema`（snake_case，不是 JSON-RPC 里的 `inputSchema`）。

**关键观察**：

1. `list_tools` 本身 < 1ms（已缓存，第二次几乎零开销）；但 `initialize` + `stdio` 子进程冷启动 ≈ **2.2s**。
2. Schema 只有 2 个工具，规模小但每个都很重（`description` 字段 200+ 字，内嵌调用规则与反例）。
3. **属性顺序固定**：`required` 和 `properties` 键顺序 5 次完全一致 → 可以安全缓存 hash 做 staleness 检测。

### 典型 inputSchema 样例（query-docs）

```json
{
  "type": "object",
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "properties": {
    "libraryId": {"type": "string", "description": "Exact Context7-compatible library ID (e.g., '/mongodb/docs', '/vercel/next.js') ..."},
    "query":     {"type": "string", "description": "What to look up... Be specific..."}
  },
  "required": ["libraryId", "query"]
}
```

## 3. `call_tool` 响应实测

### 3.1 `resolve-library-id`（fastapi）

```
isError=False, content[0].type='text', text_len=1614–1694
```

返回 markdown 列表，每条含 `Title` / `Context7-compatible library ID` / `Description` / `Code Snippets` / `Source Reputation` / `Benchmark Score`，**多条候选**（如 fastapi 同时命中 `fastapi_tiangolo` 和 `fastapi_tiangolo_reference`）。

### 3.2 `query-docs`（fastapi Depends）

```
isError=False, content[0].type='text', text_len=5491–11198（实测 6690 中位数）
```

返回 markdown，含 `### 标题`、`Source: URL`、多个 ``` 代码块、段落分隔 `--------`。

### 3.3 ⚠️ 静默 redirect（计划 §5.2 必须处理）

`query-docs(libraryId='/fastapi/fastapi', ...)` 返回：

```
text_len=89, 完整内容：
"Library /fastapi/fastapi has been redirected to this library: /websites/fastapi_tiangolo."
```

`is_error=False`，看起来像"成功响应"，**但实际没返回文档**。说明 Context7 库 ID 会随上游文档迁移而变化（fastapi 旧 ID 已重定向到 `websites/fastapi_tiangolo`）。adapter 必须：
- 把 `redirected to this library` 视为特殊错误；
- 或每次 `query-docs` 前调 `resolve-library-id` 重新解析；
- 或在 89 字节响应（明显低于 1KB）出现时自动重试。

### 3.4 性能

| 操作 | 时延 |
|---|---|
| initialize（含子进程冷启动） | 2.22s |
| list_tools | < 0.01s |
| resolve-library-id(fastapi) | 1.85s |
| query-docs(fastapi Depends) | 2.72s |
| query-docs(langchain ChatModel) | 3.02s |

→ `list_tools` 启动期缓存即可；`call_tool` 必须在 agent 路径异步执行，单次 2-3s 不能塞同步链路。

## 4. 错误响应矩阵

| 场景 | `is_error` | `content[0].text` 形态 | 是否抛 Python 异常 |
|---|---|---|---|
| 缺必填参数（`query-docs` 无 `libraryId`） | **True** | `"Input validation error: Invalid arguments for tool query-docs: libraryId: Invalid input: expected string, received undefined"` | 否（返回 CallToolResult） |
| `query-docs` 传不存在的 libraryId | **False** | `"Library \"/totally/fake\" not found. Please check the library ID or your access permissions."` | 否 |
| `resolve-library-id` 传 `libraryName=""` 空串 | **False** | 模糊匹配到 `pub_dev_test`（Dart test 框架）等 | 否 |
| `resolve-library-id` 传乱码 `libraryName` | **False** | `"No libraries found for \"asdfqwerzxcv12345\". Try a different search term."` | 否 |
| 必填 schema 校验失败（缺字段） | True | zod 风格 validation 消息 | 否 |

**关键观察**：

1. **没有一种情况会让 mcp Python 客户端抛异常**——所有错误都被包装为 `CallToolResult(content=[TextContent(...)], is_error=...)`。
3. **只有 schema 校验失败时 `is_error=True`**，其余语义错误（library not found / no match）一律 `is_error=False`。
4. adapter 必须**字符串匹配** `not found` / `No libraries found` / `redirected to this library` 才能识别失败。
5. **没有 HTTP 401 / 429**——Context7 未使用 API key 配额，也没有触发限流（至少测试人连续 10+ 次调用无问题）。

## 5. 对 plan §5.1 / §5.2 的具体影响

### 5.1 `mcp_client_manager.py`

1. **冷启动 2.2s 必须 lazy**：当前 plan 假设 lifespan 里预热所有 server。Context7 单独不算慢；但多 server 串行预热线性叠加。建议改为 `MCPToolCache.get(server_name)` 首次调用时 spawn 并 list_tools，已缓存则直接返回。
2. **stdio 子进程并发上限**：每个 server 一个常驻子进程。qa-system 计划接 context7 + microsoft-learn + 内部 ontology → 至少 3 个 stdio 子进程。lifespan 阶段全部预热 + 同时 list_tools 时，PG/Neo4j/Milvus 已有 6+ 客户端，**`asyncio.create_subprocess_exec` 上限要验证**（macOS 默认 ulimit -n 256，子进程不会爆，但 MCP server 自身也是 Node，每个 ~150 MB 内存）。
3. **熔断器**（plan 提及）：实测 Context7 无 401/429，但 2-3s 的网络延迟是常态——熔断阈值不能按 HTTP 5xx 设，要按 P95 时延。

### 5.2 `mcp_adapter.py`

4. **`tool.inputSchema` 字段名不匹配**：plan §5.2 草案写的是 `tool.inputSchema`（camelCase，JSON-RPC 原始字段），但 `mcp` Python 包 Pydantic 模型暴露的是 `tool.input_schema`（snake_case）。两种写法只有一种能跑。**需统一为 snake_case**（与项目全局 Python 风格一致，符合 `Harness/rules/编码规范.md`）。
5. **redirect 处理是 MUST**：`/fastapi/fastapi → /websites/fastapi_tiangolo` 这种静默重定向会让用户看到一句没用的话。adapter 在 `_dispatch_handler` 里要：
   - 检测 `text.startswith("Library ") and "redirected" in text`；
   - 自动重抽 `text` 里的新 ID 重新调用一次；
   - 二次失败才标 failed。
6. **libraryId 必须先 resolve**：plan 已经规划 `resolve-library-id` 作为 `query-docs` 的前置（与 MCP 自身规则一致）。但要补一条：**agent 调用工具时传入的"库名"是自然语言**，agent 不会主动先 resolve。adapter 必须把 `mcp:context7:query-docs` 的 `libraryId` 标记为"由 helper 自动填充"，agent prompt 只暴露 `libraryName + query` 两个槽。
7. **错误判定不能依赖 `is_error`**：必须 inspect 文本。约定 adapter 内统一识别这几条：
   - `is_error=True` → 协议级失败（缺参 / schema 不匹配），重试无意义
   - 文本含 `"not found"` / `"No libraries found"` / `"redirected to this library"` → 业务级失败，可重试或换 ID
8. **不引入 API key 配置**：plan §5.1 假设 `mcp_servers.yaml` 有 `api_key` 字段。Context7 实测不需要，YAML schema 应允许 `api_key` 缺失（可选字段，且缺省时传空 env）；后续接付费 server（如 Firecrawl）再启用。

### 跨工具契约

9. **缓存 key = sha1(server_url + tool_name + properties_key 顺序)**：5 次跑 schema properties 顺序完全稳定，可做强缓存避免每次冷启都 list_tools。
10. **日志埋点**：每次 call_tool 记 4 个字段——server / tool / latency_ms / text_len / outcome（ok | validation_error | not_found | redirect | timeout）——便于审计和熔断。

## 6. 结论一句话

Context7 **可接、零成本、稳定**，但**silent redirect + is_error 永远 False** 两个语义陷阱会让 §5.2 的"零代码接入"在第一次 fastapi 文档查询时翻车——adapter 必须显式处理这两种失败形态，并强制 `libraryId` 前置 resolve。