# Task 1.1b 报告 — 修 `SessionQueryState.user_id` 的 str/int 类型污染（Critical，热修）

- **分支**：`fix/aichat-gap-f1-stream-scope`（基线 `25661580`，同分支连续开发）
- **状态**：DONE
- **改动规模**：3 个文件（2 个生产文件各 9+2 处替换，1 个新测试文件），`chat_service.py` 行数不变（1987 行）

---

## 1. 结论速览

| 项 | 结果 |
|---|---|
| RED 测试（3 条） | 3 failed → 3 passed |
| `test_chat_api.py` | 基线 8 failed/14 passed → **0 failed / 14 passed** |
| 定向 integration 回归 | 1 pre-existing failed（未因本次改动引入）|

---

## 2. 改了哪些位置

### 2.1 `app/services/chat_service.py`（9 处，均为 `user.userId` → `user.dbUserId`）

| 行号 | 上下文 |
|---|---|
| 436 | `_classifyMessage` 调用 → `userId=` |
| 526 | `_executeMultiStep` 调用（prior_snapshot 分支） |
| 546 | `_executeMultiStep` 调用（compound question 分支） |
| 561 | `_executeMultiStep` 调用（LOOKS_LIKE_COMPOUND 分支） |
| 582 | `_executeMultiStep` 调用（追问重试分支） |
| 642 | `_executeMultiStep` 调用（异常回退分支） |
| 712 | `_saveQueryState` 调用（查询成功分支） |
| 1685 | `_saveQueryState` 调用（预计算特征命中分支） |
| 1742 | `_saveQueryState` 调用（语义搜索命中分支） |

### 2.2 `app/services/chat_stream.py`（2 处）

| 行号 | 上下文 |
|---|---|
| 923 | `_rejectOversizedPlan` 调用（步数超限分支） |
| 1233 | `_finalizeMultiStepDegrade` 调用（多步降级分支） |

### 2.3 未改的两处（按设计）

- `chat_service.py:374`：`setChatUserId(user.userId if ...)` — 审计 ContextVar，签名是 `str | None`
- `chat_stream.py:146`：`setChatUserId(user.userId if ...)` — 同上

---

## 3. RED 阶段真实报错

```
sqlalchemy.exc.ProgrammingError: (sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError)
<class 'asyncpg.exceptions.UndefinedFunctionError'>: operator does not exist: bigint = character varying
HINT:  No operator matches the given name and argument types. You might need to add explicit type casts.
[SQL: SELECT session_query_state.id, session_query_state.session_id,
       session_query_state.datasource_id, session_query_state.schema_name,
       session_query_state.user_id, ...
 FROM session_query_state
 WHERE session_query_state.session_id = $1::VARCHAR
   AND session_query_state.user_id = $2::VARCHAR      ← str 填进了 bigint 列
   AND session_query_state.datasource_id = $3::BIGINT]
[parameters: ('s1', 'anonymous', 99999)]
```

修复后 3 条测试全部 GREEN。

---

## 4. 新增测试文件

`app/tests/integration/test_chat_query_state_user_id_type.py`，3 条用例：

1. **`test_non_streaming_persists_user_id_as_int`**：走 `POST /api/v1/chat` 完整链路，验证 user_id 是 `int`，不是 `str`。
2. **`test_stub_user_no_match_persists_null`**：X-User-Id 无法解析到 DB 用户，验证 user_id 为 NULL 且不抛 500。
3. **`test_dbuserid_none_does_not_store_string`**：反向断言，X-User-Id = "not-an-int"（dbUserId=None），验证落库列是 NULL 而非字符串。

fixture 复用：`_seed` / `_installFakes` / `_PipelineLlm` / `_FakeAdapter` / `_RouterFor`（均来自 `test_chat_api.py`）。

**修复 `_StubEmbeddingService` 缺口**：补全 `generateEmbedding`（`list[float]`）和 `searchSimilarQueries`（`list`），否则 `EmbeddingService._ensureClient` 调用时在 `embedWithUsage` 路径报错（实际生产代码里是 fire-and-forget，但 embedding_client.py 的 `embedWithUsage` 路径在假客户端下会抛连接错误 —— 实际是 warning 不阻断，但 `_StubEmbeddingService` 缺少这些方法本身是另一个缺失，本次不扩）。

---

## 5. Gate 结果

### Gate 1：RED → GREEN
```
$ uv run pytest app/tests/integration/test_chat_query_state_user_id_type.py -q -p no:randomly
3 passed, 21 warnings
```

### Gate 2：`test_chat_api.py` 0 failed / 14 passed
```
$ uv run pytest app/tests/integration/test_chat_api.py -q -p no:randomly
14 passed, 21 warnings
```
（基线 8 failed/14 passed → 修复后 0 failed/14 passed）

### Gate 3：定向回归（无新增失败）
```
$ uv run pytest test_chat_api.py test_chat_stream_api.py test_chat_stream_query_state_scope.py test_multistep_global_filter.py -q -p no:randomly
FAILED ...::test_c_fallback_multistep_inherits_global_constraints  ← pre-existing，与本次改动无关
31 passed, 1 pre-existing failed
```

验证 pre-existing：在 F1 基线（`git stash` 后）同一命令得到完全相同的失败。

---

## 6. 项目约束核对

| 约束 | 结果 |
|---|---|
| 不可变对象 | 所有替换均创建新值，未原地修改 |
| 真实 PG + 完整 API 链路 | 5434 `qa_metadata_test`；无 sqlite 内存库；无直调 service |
| 不改 `setChatUserId` 那两处 | 已确认不动 |
| `chat_service.py` 行数 | 1987 → 1987（未触碰该文件行数增长）|
| `camelCase` / `PascalCase` | 均符合 |
| 只改 11 处指定位置 | 已逐行核对 |
