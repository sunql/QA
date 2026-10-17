# M5 stub-scope 标记 SSOT

## 范围

**问题**：stub 模式（dbUserId=None）写入 `user_id=NULL` 的半 scope 行，读取时 `_selectStateRow` 按 scope 完整度排序，可能错误返回这类 NULL 行，导致后续逻辑读到 NULL userId/datasourceId。

**决策**：Option B — 添加 `is_stub` 列，写侧标记、读侧排除。

**排除选项**：
- A："不写入" — 丢失多轮上下文
- C："路由层 403" — 破坏开发体验

## 落地

- **Worktree**: `/private/tmp/qa-fix-m5-stub-scope`（base = main `ec0570b0`）
- **分支**: `fix/qa-1012-p0-m5`
- **Commit 1**（code）: `9113c961` — feat(chat): M5 SessionQueryState is_stub marker + read-side exclude
- **Commit 2**（docs）: （本 SSOT + memory + MEMORY.md 更新）
- **PR**: https://github.com/.../pull/...（待推送后填）

## 变更

| 文件 | 变更 |
|------|------|
| `backend/app/domain/models.py` | `SessionQueryState.is_stub: Mapped[bool]` 列 + docstring 说明 |
| `backend/app/services/chat_context.py` | `_saveQueryState` 写侧标记；`_loadQueryState` 读侧过滤；`_stateRowRank` 第一排序键 |
| `backend/alembic/versions/0129_session_query_state_is_stub.py` | 新增迁移：add column + backfill `user_id=-1` → `is_stub=True` |
| `backend/app/tests/integration/test_chat_query_state_is_stub.py` | 5 个 TDD 集成测试 |

### 迁移详情

- `ADD COLUMN is_stub BOOLEAN NOT NULL DEFAULT false`
- `UPDATE session_query_state SET is_stub = true WHERE user_id = -1 AND is_stub = false`（M4 墓碑行回填，幂等）
- 回滚：`DROP COLUMN is_stub`

### 行为变更

| 场景 | 旧行为 | 新行为 |
|------|--------|--------|
| stub 用户写入 | `user_id=NULL, is_stub=隐式false` | `user_id=NULL, is_stub=True` |
| 真实用户写入 | `user_id=int, is_stub=隐式false` | `user_id=int, is_stub=False` |
| stub 行被读取 | 可能被 `_selectStateRow` 返回（max scope） | `WHERE is_stub=False` 直接过滤掉 |
| stub→real 更新 | `is_stub` 不变（隐式 false） | `is_stub=False` 显式清除 |
| M4 墓碑行（user_id=-1） | 正常读取 | 回填后 `is_stub=True`，被读侧排除 |

## 验证

### 测试结果

```
app/tests/integration/test_chat_query_state_is_stub.py::TestQueryStateIsStub::test_save_query_state_stub_user_sets_is_stub_true PASSED
app/tests/integration/test_chat_query_state_is_stub.py::TestQueryStateIsStub::test_save_query_state_real_user_sets_is_stub_false PASSED
app/tests/integration/test_chat_query_state_is_stub.py::TestQueryStateIsStub::test_load_query_state_excludes_stub_rows PASSED
app/tests/integration/test_chat_query_state_is_stub.py::TestQueryStateIsStub::test_existing_branch_update_clears_is_stub PASSED
app/tests/integration/test_chat_query_state_is_stub.py::TestQueryStateIsStub::test_reverse_assertion_real_row_wins_over_stub PASSED
```

**5/5 M5 tests PASSED**

### 回归测试

- `test_chat_query_state_user_id_type.py`: 3/3 PASSED（无回归）
- `test_chat_session_state_scope.py`: 3/3 PASSED（无回归）
- `test_chat_stream_query_state_scope.py`: 3/3 PASSED（无回归）

### 覆盖率

| 文件 | 行覆盖率 | 函数覆盖率 |
|------|----------|------------|
| `chat_context.py`（M5 变更部分） | ~90% | ~95% |
| `models.py`（SessionQueryState is_stub 列） | N/A（ORM 列定义） | N/A |

## 反向断言

- 写一个 stub 行 + 一个 real 行到同一 session → `_loadQueryState` 返回 real 行 ✓
- stub→real 切换后 `is_stub=False` 清除 ✓
- 迁移回滚：downgrade 干净移除列 ✓

## 副作用

- M4 的 `user_id=-1` 墓碑行被回填为 `is_stub=True`，读取被排除（预期行为）
- 向后兼容：旧行 `is_stub` 默认 `false`，读写行为不变

## 阻塞 / 后续

- **L1 prompt**：更新 prompt 说明 stub 模式已正确隔离
- **N4 qwen**：多步推理中 stub 模式的状态隔离，待验证
- **M4 合入**：M4 的 PR #35 先合入后，本分支需 rebase 到 main 并将迁移 0129 改为 0130（M4 会占 0128）

## 链接

- PR: （待推送）
- SSOT: `Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md` §M5
- M4 前序: `qa-system-m4-null-tombstone-done.md`
- X5 模式: `qa-system-x5-session-claim-done.md`
