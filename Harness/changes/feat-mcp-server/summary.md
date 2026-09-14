# 变更：Phase 6 MCP Server（Model Context Protocol）

- **日期**：2026-09-14
- **作者**：AI 助手
- **状态**：done
- **触发**：迁移 llm_wiki-main 能力矩阵中的 MCP Server（llm_wiki_main 是把知识能力暴露给 LLM Agent 的标准模式）

## 1. 背景

qa-system 此前只能通过 REST API 与外部系统集成。LLM Agent 生态（Claude Code /
Cursor / Cline 等）走 MCP（Model Context Protocol）标准协议：把工具列表 +
JSON-RPC schema 注册到客户端，Agent 自然理解每个工具的参数与返回值，比 REST
更适合「Agent 自主调用」场景。

迁移目标：把 qa-system 已有的「wiki 知识能力」打包为 10 个 MCP 工具，让外部
Agent 能复用同一份知识图谱检索 + 受控写入能力，避免每个 Agent 自己重写一份
REST 调用样板。

## 2. 设计

### 2.1 复用 > 重写

10 个工具 **全部** 走 qa-system 已有 service（`WikiPageService` /
`listCommunities` / `insight_service` / `AutoClassifier` / `RelationDiscovery` /
`CommunityTopicSuggester`）+ 已有 endpoint（`/classify/preview`、
`/relations/suggest`、`/topic-suggest`、`PATCH /pages/{id}`、
`PATCH /communities/{key}`）。

不重复实现：MCP 工具层只做：
1. **DB session + stub auth 上下文拼装**（`McpContext`）
2. **响应截断**（`_MAX_RESPONSE_CHARS = 50KB`，避免 LLM 上下文塞爆）
3. **JSON-RPC envelope**（FastMCP 自动处理）

### 2.2 双 transport：stdio + streamable-http

| 模式 | 用途 | 入口 |
|------|------|------|
| **stdio** | Claude Code / Cursor 等本地客户端（标准 MCP） | `python -m app.services.mcp_server --transport stdio` |
| **streamable-http** | 远程 Agent / 微服务集成 | `POST /mcp`（FastAPI mount 到 `/mcp` 路径） |

stdio 模式用 `MCP_USER_ID` 环境变量注入 stub auth user；HTTP 模式走现有
stub auth 链路（`_openContext()` 内显式构造 `CurrentUser`）。

### 2.3 10 个工具分类

**只读（3 个）**：
- `wiki_status` — DB 可达 + 当前 stub auth 用户身份（健康检查 + 鉴权体检）
- `wiki_search(query, dimension?, limit)` — ILIKE 模糊检索（标题/正文）
- `wiki_read(page_id)` — 拉取 page + claims + relations

**图分析（2 个）**：
- `wiki_graph_communities()` — Louvain 社区列表（含 topic 字段）
- `wiki_graph_insights(gap_kind?, limit)` — 知识缺口 + 意外连接 + 桥接节点

**预览（3 个，写入前的强制确认）**：
- `wiki_preview_classify(page_id)` — MISSING_DIMENSION 重分类建议
- `wiki_preview_relations(page_id)` — ISOLATED_PAGE 关联候选
- `wiki_preview_community_topic(community_key)` — SPARSE_COMMUNITY 主题建议

**写入（2 个，对应预览确认）**：
- `wiki_update_dimension(page_id, dimension, auto_classification?)` — 落库分类
- `wiki_update_community_topic(community_key, topic)` — 落库社区主题

预览/写入分离：符合 Phase 5.5 的「两步预览」原则（详见
`feat-wiki-gap-actions/summary.md`）。

## 3. 关键实现

### 3.1 lifespan 合并（HTTP 模式最大坑）

`fastmcp.http_app()` 返回的 `StarletteWithLifespan` 自带 lifespan（管理
`StreamableHTTPSessionManager` task group）。挂载到 FastAPI 时必须把这个
lifespan 合并进父 app —— 否则启动后第一个 HTTP 请求触发
`RuntimeError: StreamableHTTPSessionManager task group was not initialized`。

```python
# app/main.py
_mcpApp = _mcpServer.http_app(path="/mcp", transport="streamable-http")
app.router.routes.append(Mount("", app=_mcpApp))

@asynccontextmanager
async def _mergedLifespan(fastapiApp: FastAPI) -> AsyncIterator[None]:
    async with lifespan(fastapiApp):              # 原 FastAPI lifespan
        async with _mcpApp.lifespan(_mcpApp):     # FastMCP 子 lifespan
            yield

app.router.lifespan_context = _mergedLifespan
```

关键点：
- `_mcpApp.lifespan(_mcpApp)` 调用方式：`_mcpApp.lifespan` 是返回
  `_AsyncGeneratorContextManager` 的 callable，必须传 `_mcpApp` 实例参数
- `app.router.lifespan_context = ...` 必须在 `createApp()` 内部赋值
  （uvicorn 启动时读取 `app.router.lifespan_context`）

### 3.2 stub auth 上下文

MCP 不走 FastAPI Header 注入，stub auth 必须手动构造：

```python
from app.dependencies import _buildCurrentUser, getCurrentUser

base = _buildCurrentUser(
    userId=_DEFAULT_USER_ID,
    tenantId="default",
    rolesHeader=None,  # 走 DEFAULT_STUB_ROLES 默认值（含 admin）
    departmentsHeader=None,
)
current = await getCurrentUser(
    xUserId=_DEFAULT_USER_ID,
    xTenantId=base.tenantId,
    xUserRoles=",".join(base.roles),
    xUserDepartments=None,
    session=session,
)
```

注意 `_buildCurrentUser` 4 个 kw-only 参数全必填（userId / tenantId /
rolesHeader / departmentsHeader）。早期漏传 3 个参数导致 500。

### 3.3 响应截断

50KB 上限：保护 LLM 上下文（典型 200k token 窗口，1 个工具返回 50KB ≈ 12k
token = 6%）。`_truncate()` 在末尾加 `[truncated: N bytes omitted]` 标记。

```python
_MAX_RESPONSE_CHARS = 50_000

def _json(data, limit=_MAX_RESPONSE_CHARS) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str, indent=2)
    return text[:limit] + f"\n\n[truncated: {len(text)-limit} bytes omitted]" \
        if len(text) > limit else text
```

## 4. 验证

### 4.1 stdio 模式

```
$ echo '{"jsonrpc":"2.0","id":1,"method":"initialize",...}' | \
  MCP_USER_ID=1 python -m app.services.mcp_server --transport stdio
{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"qa-system-wiki","version":"4.0.3"},...}}
```

serverInfo + capabilities + instructions 全部正确序列化。

### 4.2 HTTP 模式（FastMCP 客户端）

```
Client('http://127.0.0.1:8765/mcp')
```

| Tool | 验证结果 |
|------|----------|
| `list_tools` | 10 tools 全部注册 |
| `wiki_status` | `{"ok":true,"userId":"anonymous","authMode":"stub"}` |
| `wiki_graph_communities` | `[]`（DB 无社区） |
| `wiki_search 供应商` | **真实数据**：30 结果，含「采购供应商黑名单管理规范」等 |
| `wiki_graph_insights` | 真实 ISOLATED_PAGE gap 列表 |
| `wiki_read PAGE-UNTITLED-B145A929` | 完整 page + claims + relations |
| `wiki_preview_classify nonexistent-id` | ToolError ✓（FastMCP 把 NotFoundError 转 is_error=true） |
| `wiki_preview_community_topic NONE` | ToolError ✓（同上） |
| `wiki_update_community_topic NONE` | ToolError ✓（同上） |

### 4.3 集成测试（14/14 PASS）

`app/tests/integration/test_mcp_server.py`：

| 测试 | 覆盖 |
|------|------|
| `test_openContext_returnsValidSession` | DB session + currentUser 正确建立 |
| `test_openContextClosesOnError` | async with 异常路径自动 rollback + close |
| `test_wiki_status_returnsOk` | 状态字段 + authMode |
| `test_wiki_search_returnsMatches` | 真实 wiki 数据命中 |
| `test_wiki_read_returnsPageContent` | page + content + claims + relations |
| `test_wiki_graph_communities_returnsList` | community 列表 |
| `test_wiki_read_pageNotFound_raisesNotFound` | 错误契约一致（raise DomainError，不返 success JSON） |
| `test_wiki_preview_classify_pageNotFound_raisesNotFound` | 同上 |
| `test_wiki_preview_classify_noActiveModel_raisesNotFound` | 无 active LlmConfig 报错 |
| `test_wiki_preview_community_topic_notFound_raisesNotFound` | community 不存在报错 |
| `test_wiki_update_community_topic_notFound_raisesNotFound` | update 路径同契约 |
| `test_wiki_update_dimension_writesAndCommits` | 真持久化（verify_session 重读） |
| `test_wiki_update_community_topic_writesAndCommits` | 同上 |
| `test_all_ten_tools_registered` | 10 tools 集合回归保护 |

回归：Phase 5.5 `test_wiki_gap_actions_api.py` 11/11 PASS（无破坏）。

### 4.4 lifespan + startup 链路

启动日志验证 schema drift、seed、agent binding cache、tool config registry、
feature rule registry、business object registry、KPI cache、menu seed、
RBAC seed 全部完成 → MCP session manager 启动 → ready to serve。

## 5. 客户端集成示例

### 5.1 Claude Desktop / Cursor `claude_desktop_config.json`

```json
{
  "mcpServers": {
    "qa-system-wiki": {
      "command": "/path/to/venv/bin/python",
      "args": ["-m", "app.services.mcp_server", "--transport", "stdio"],
      "env": {"MCP_USER_ID": "1", "PYTHONPATH": "/path/to/backend"}
    }
  }
}
```

### 5.2 远程 Agent（HTTP）

```python
from fastmcp import Client

async with Client("http://qa-system.internal:8000/mcp") as client:
    insights = await client.call_tool("wiki_graph_insights", {"limit": 10})
    pages = await client.call_tool("wiki_search", {"query": "供应商", "limit": 5})
```

## 6. 安全

- **生产环境 stub auth 必关**：`AUTH_STUB_ENABLED=0`，反向代理剥离 `X-User-*` 头
  —— MCP `wiki_status` 工具本身用于体检，可作为部署前 smoke test
- **写工具受限**：所有写入都对应 Phase 5.5 预览流程（preview 工具不写库 →
  update 工具落库），Agent 不能绕过预览直接污染知识网络
- **错误契约一致**：所有 tool 资源不存在 → 抛 DomainError（NotFoundError）→
  FastMCP 转 `is_error=true` 的 ToolError 给客户端。**禁止**返回
  `{"error": "..."}` + `is_error=False` 的混淆 JSON
- **响应截断**：单次响应 ≤ 50KB，避免 Agent 拖光上下文

## 7. 改动文件清单

**新增**：
- `backend/app/services/mcp_server.py` — 10 个工具 + 双 transport 入口（~660 行）
- `backend/app/tests/integration/test_mcp_server.py` — 14 个集成测试
- `Harness/changes/feat-mcp-server/summary.md` — 本文档

**修改**：
- `backend/pyproject.toml` — 加 `fastmcp>=2.0.0` 依赖
- `backend/app/main.py` — MCP 挂载 + 合并 lifespan（_mcpApp.lifespan(_mcpApp)
  + app.router.lifespan_context 重赋值）

## 8. 后续（可选）

- **多租户隔离**：MCP 工具当前共享 stub auth `anonymous`；生产部署需根据
  `MCP_USER_ID` env 或 HTTP header 隔离租户
- **Tool 鉴权分级**：`wiki_update_*` 工具应要求 `admin` role；当前走默认
  admin stub，鉴权声明但未生效（生产 stub 关闭前必须补 ACL check）
- **Resource 注册**：FastMCP 支持 `@mcp.resource(uri=...)` 暴露只读资源
  （如 `wiki://pages/{id}`）—— 当前 10 个工具覆盖足够，未启用