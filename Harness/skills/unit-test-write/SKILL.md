---
name: unit-test-write
description: TDD 测试编写（RED -> GREEN -> REFACTOR）
---

# 单元测试编写技能

## TDD 工作流
1. **RED**：写失败测试，覆盖正常/边界/异常。
2. **GREEN**：最小实现通过。
3. **REFACTOR**：在不破坏测试前提下优化。

## 结构（AAA）
```python
def test_xxx(self) -> None:
    # Arrange
    ...
    # Act
    ...
    # Assert
    ...
```

## 命名
`test_<行为>_<条件>`，描述被测行为：
- `test_returns_zero_for_empty_text`
- `test_raises_when_all_disabled`
- `test_excludes_model_exceeding_threshold`

## 后端
- pytest + pytest-asyncio（`asyncio_mode=auto`）。
- **数据层必须真实 PostgreSQL**（禁止 sqlite 内存库，强制规则见 `rules/测试规范.md`）：conftest 连独立测试库（`qa_metadata_test`）+ Alembic `upgrade head` 建表，每个测试独立清库。
- **完整 API 链路**：用 `client`（AsyncClient + ASGITransport）从 HTTP 入口测试，经路由/中间件/service/repository/真实 PG，禁止直接调用 service 代替。仅外部依赖（LLM/Milvus/Neo4j/外部 HTTP）允许 test double。
- 注入依赖而非 mock 内部（如 OpenAiClient 接受 `client` 参数）。
- 覆盖率：`pytest --cov=app --cov-fail-under=80`。

## 前端
- Vitest + React Testing Library + MSW。
- `vitest run --coverage`，阈值 80%。

## 测试隔离
每个测试独立 DB 会话，不依赖执行顺序。
