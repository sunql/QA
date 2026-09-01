# 变更：fix-neo4j-async-traverse

- **日期**：2026-09-01
- **阶段**：Phase 7 G1（Agent Runtime 遗留缺口）
- **类型**：缺陷修复（性能/异步安全）
- **提交**：`fix: neo4j graph traversal runs in thread to avoid blocking event loop`

---

## 1. 问题

`GraphTraversalService.traverse()` / `traverseForChat()` 是 async 方法，但内部直接调用同步 Neo4j 官方驱动（`neo4j_client.getBusinessNode`、`neo4j_client.traverseBusinessGraph`）。同步 CQL 查询会阻塞 uvloop 事件循环，导致：

- 高并发时其他请求被图查询拖住；
- 与 FastAPI / async SQLAlchemy 的异步架构不一致；
- `feat-agent-runtime-mvp` §10 明确列为 LOW#4 遗留缺口。

## 2. 设计决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 驱动层 | 保留同步 `neo4j` 官方驱动 | 无新依赖、无连接池重构、与 `graph.py` 本体图路径保持一致 |
| 包装方式 | `asyncio.to_thread()` | Python 3.9+ 标准方案，把同步 I/O  offload 到线程池，不阻塞事件循环 |
| 改造范围 | 仅业务图遍历路径 | 最小改动；本体图（Class/Property/Metric）写操作量小，暂不改造 |
| API 签名 | `traverse` / `traverseForChat` 改为 `async` | 调用链同步修改：API endpoint、chat_service、agent_tools |

## 3. 数据模型变更

无 DB 迁移、无 schema 变更。

## 4. 关键代码

### 后端修改

- `app/services/graph_traversal_service.py`：
  - `traverse` → `async def traverse`，内部 `getBusinessNode` / `traverseBusinessGraph` 用 `asyncio.to_thread(...)` 包装；
  - `traverseForChat` → `async def traverseForChat`，`await self.traverse(...)`。
- `app/api/v1/graph_traversal.py`：`return await service.traverse(...)`。
- `app/services/chat_service.py`：`_handleGraphReasoning` 中 `await self._graphTraversal.traverseForChat(...)`。
- `app/services/agent_tools.py`：`_graphTraverseHandler` 中 `await service.traverseForChat(...)`（Agent Runtime 调用图遍历工具路径）。

### 测试修改

- `app/tests/unit/test_graph_traversal_service.py`：
  - 所有调用 `traverse` / `traverseForChat` 的用例改为 `async def` + `await`；
  - 新增 `test_traverse_does_not_block_event_loop`：并发执行 traverse 与其他 task，断言其他 task 能并行完成。

## 5. 测试

| 层 | 文件 | 结果 |
|---|---|---|
| 单元 | `test_graph_traversal_service.py` | 22 passed |
| 集成 | `test_graph_traversal_api.py` | 10 passed |
| 集成 | `test_agent_runtime_api.py` | 12 passed |
| 全量后端 | `app/tests/ --cov=app` | 1861 passed, 1 skipped，覆盖率 93.22% |
| 前端 | `tsc --noEmit` + vitest | 0 errors / 348 passed |

## 6. 安全审查

- 无用户输入进入 CQL，参数仍使用 `$param` 绑定；
- 白名单校验仍在同步函数内执行，未绕过；
- 无新依赖、无新增密钥。

## 7. 验证

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit/test_graph_traversal_service.py \
    app/tests/integration/test_graph_traversal_api.py \
    app/tests/integration/test_agent_runtime_api.py -v
```

期望：44 passed。

## 8. 关键文件清单

- `backend/app/services/graph_traversal_service.py`
- `backend/app/api/v1/graph_traversal.py`
- `backend/app/services/chat_service.py`
- `backend/app/services/agent_tools.py`
- `backend/app/tests/unit/test_graph_traversal_service.py`
