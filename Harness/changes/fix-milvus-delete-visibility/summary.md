# 变更：fix-milvus-delete-visibility

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：Milvus delete visibility 延迟（测试稳定性 + L2 unreachable）
- **状态**：done
- **关联变更**：`fix-plan-scope-gate-and-trigram`（上批 C 部分 random id 已落）；`chat-service-assessment.md` §2.4 L2（unreachable）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.4 L2 + 上批 SSOT §7 实测警示

---

## 1. 需求

### A. Milvus delete visibility 延迟（集成测试不稳定）

集成测试 `test_delete_scopes_to_one_page` / `test_delete_document_chunks_scopes_to_one_document` 在 delete 后 query 仍读到残留行。

**根因**：Milvus delete buffer 走 `Proxy → DML channel → DataNode 落盘 delta binlog → QueryNode 拉取并应用 bitset` 三段异步管道。`flush()` 仅把 **Growing → Sealed → Flushed**（持久化到对象存储），**不保证 QueryNode 已加载并应用 delta**。

**实测**（PyMilvus 2.4.6 + Milvus v2.4.6 服务端，collection 默认 Bounded）：

| 方法 | delete 后立即 query |
|---|---|
| `consistency_level="Strong"`（per-request kwarg） | ❌ **仍读到残留**（Bounded 5s 容忍窗口覆盖 per-request 覆盖） |
| `consistency_level="Strong"` + `guarantee_timestamp` | `get_server_timestamp` 在 PyMilvus 2.4.6 **不存在** |
| `utility.wait_for_flush_completed` | PyMilvus 2.4.6 **不存在**（PyMilvus ORM 只暴露 `flush_all` / `wait_for_index_building_complete` / `wait_for_loading_complete`） |
| `release() + load()`（reload 强制刷新） | ✅ **立即读到删后状态**（实测 ~2.3s） |

> **plan 时 agent 给的方案 A/B 与代码实测不符**：`flush()` 在 PyMilvus 2.4.6 ORM 中**不返回 dict**（返回 None）；`wait_for_flush_completed` / `get_server_timestamp` 在 PyMilvus 2.4.6 中**不存在**。修法改为 `release + load` 强制 QueryNode 刷新。

### B. L2 unreachable 改 NoReturn-marker

`llm_retry_policy.py:171` `raise RuntimeError("unreachable")`——ruff B008 警告 + 类型不准确。

---

## 2. 设计评审

### 方案选择

| 候选 | 选定 | 理由 |
|---|---|---|
| `consistency_level="Strong"` per-request 覆盖 | ❌ 实测不生效 | Bounded 5s 窗口覆盖；PyMilvus 2.4.6 无 `wait_for_flush_completed`/`get_server_timestamp` 兜底 |
| `assert False` 代替 `raise RuntimeError` | ❌ ruff 仍报（生产环境 `-O` 模式会被剥离） | 仅 `assert False, "msg"` 不足以逃过 ruff 静态分析 |
| `raise RuntimeError("unreachable")` + `noqa: B008` 注释 | ✅ L2 选定 | 显式说明「这是给 mypy 的 NoReturn marker，不是真实 raise」 |
| `release + load` reload helper（`refreshWikiCollection` / `refreshDocumentCollection`） | ✅ Milvus 选定 | 实测 ~2.3s 让 delete 立即对 query 可见；新增 helper 隔离测试/生产语义 |
| `consistency_level` 参数保留在 query/search helper | ✅ future-proof | 服务端未来支持时立即生效；测试场景同时走 reload 兜底 |

### 不做

- **不删 `consistency_level` 参数**：保留作为 future-proof + API 契约一致
- **不在 production code 加 reload**：reload 2-3s 延迟对生产对账路径不可接受
- **不引入 `NoReturn` 类型注解**：`assert False` 已被 ruff 拒绝，`noqa: B008` + 注释说明意图更直接

---

## 3. 数据模型变更

无。

---

## 4. 接口契约变更

| 面 | 变更 |
|---|---|
| `queryWikiPageChunks` / `queryDocumentChunks` | 新增 `consistency_level: str \| None = None` 参数（per-request 透传给 Milvus gRPC） |
| `searchDocumentChunks` | 新增 `consistencyLevel: str \| None = None` 参数（同上） |
| `refreshWikiCollection` / `refreshDocumentCollection`（NEW） | 测试专用：`release() + load()` 强制 QueryNode 刷新 delta binlog |
| `_retryWithBackoff` 末尾 unreachable | `raise RuntimeError("unreachable")` 加 `noqa: B008` + 注释说明意图 |

---

## 5. 实现要点

### Milvus query/search helper 改一致性级参数

```python
# queryWikiPageChunks
kwargs: dict[str, Any] = {"limit": _MILVUS_QUERY_PAGE}
if consistency_level is not None:
    kwargs["consistency_level"] = consistency_level
return collection.query(..., **kwargs)
```

`searchDocumentChunks` 同步处理。

### 新增 refresh helper

```python
def refreshWikiCollection() -> None:
    """测试专用：release + load 强制 QueryNode 刷新 wiki collection。

    Milvus delete buffer 走 Proxy → DML channel → DataNode → QueryNode 三段
    异步管道；flush() 仅持久化，不保证 QueryNode 已加载并应用 delta binlog。
    实测在 collection 默认 Bounded（5s 容忍窗口）下，per-request
    consistency_level="Strong" 不能立即看到 delete 后状态。

    release + load 强制 QueryNode 重新装载 segment + delta binlog，delete
    立即对后续 query 可见（实测 < 3s 完成）。

    生产对账不要用 —— 2-3s 延迟过大。仅集成测试 delete-then-query 验证场景。
    """
    collection = ensureWikiPageCollection()
    collection.release()
    collection.load()
```

### L2 unreachable

```python
# noqa: B008 —— 不是函数调用，是给 mypy 的 NoReturn marker，
# 此分支实际不会执行（tenacity 契约：始终 raise 或 yield）。
raise RuntimeError("unreachable")  # noqa: B008
```

---

## 6. 测试

| 层 | 范围 | 结果 |
|---|---|---|
| **新增守卫** | `test_milvus_wiki_fields.py` / `test_milvus_document_fields.py` delete scope 用例 | refresh + query |
| **回归** | Milvus round-trip 7 例（3 wiki + 4 document） | 7 passed × 3 run = 21/21（无累加） |
| **回归** | `test_fallback_backoff.py` | 14 passed |
| **回归** | `test_nl2sql_plan_gate.py`（上批守卫） | 7 passed |
| **ruff** | 4 文件 | 0 warning |

### 关键实测（Milvus 2.4.6）

```
=== Run 1 ===   7 passed, 110 warnings in 44.36s
=== Run 2 ===   7 passed, 110 warnings in 44.99s
=== Run 3 ===   7 passed, 110 warnings in 42.90s
```

**3 次连续 7/7 全绿**，累加彻底消除（上批 random id 已消除跨轮累加；本批 refresh 消除单次残留）。

---

## 7. 安全审查

### 收益（测试稳定性）

- Milvus round-trip 用例从「环境潜伏 + 跨轮累加」双线 flake → **3 次连续稳定通过**
- 测试场景改用 reload helper 显式语义化（vs 原 `consistency_level="Strong"` 隐性 + 无效）
- production code 不动（reload 2-3s 延迟过大）

### 边界与已知限制

- **per-request consistency_level 实测在 Bounded collection 5s 窗口内不生效**：保留参数作为 future-proof
- **PyMilvus 2.4.6 缺 `wait_for_flush_completed` / `get_server_timestamp`**：plan agent 给的方法在 2.4.6 不存在；升级到 2.5+ 后可改用
- **reload 实测 ~2.3s**：单用例 +3s 是测试成本上限，可接受
- **production 路径不动**：`wiki_vector_service.syncPage` 用 delete+insert 同 page_id 无中间读窗口，无可见性延迟风险；`searchDocumentChunks` 在 production 走默认 Bounded 不被本批影响

### 残差

- **生产 `wiki_vector_service.syncPage` 同源风险**：delete + insert 之间窗口内 search 可能命中旧 chunk；当前 production 未见事故，**登记待评估**
- **`consistency_level` 参数保留但未验证有效**：未来服务端支持 Strong per-request 覆盖时立即生效

---

## 8. 部署验证（2026-09-27）

- test 库 `alembic current` 不变（无 schema 变更）
- prod 库 `alembic current` 不变
- `/api/v1/health` 双通道 200
- Milvus 集成测试 7/7 × 3 run 全绿

---

## 9. 真实数据验证

| 项 | 结果 |
|---|---|
| 真实 Milvus round-trip（重启 Milvus 清 buffer 后） | 7/7 × 3 run 全绿 |
| `release + load` 实测耗时 | 2.3s（单次） |
| `consistency_level="Strong"` 实测有效性 | ❌ Bounded collection 5s 窗口内不生效 |

---

## 10. 关联

- commit（待提交）：
  - `fix: milvus query/search helper 增 consistency_level 参数 + release-load refresh helper`
  - `test: milvus round-trip delete 场景用 refreshWikiCollection/refreshDocumentCollection`
  - `fix: _retryWithBackoff 末尾 unreachable 加 noqa B008 注释`
- 上批 commit `29fcfab`：random id 消除跨轮累加；本批 refresh 消除单次残留；两层共同闭合 Milvus delete visibility 问题
- 评估文档更新：`Harness/wiki/chat-service-assessment.md` §2.4 L2 标注「已修」
- memory 登记：`qa-system-milvus-delete-visibility-flake` 已存在（背景）；本批新增实测警示更新在原 memory + 新增 `qa-system-milvus-strong-consistency-gotcha`

---

## SSOT 校验清单

- [x] `queryWikiPageChunks` / `queryDocumentChunks` 新增 `consistency_level` 参数
- [x] `searchDocumentChunks` 新增 `consistencyLevel` 参数
- [x] `refreshWikiCollection` / `refreshDocumentCollection` 新增
- [x] 集成测试 delete 场景用 refresh helper
- [x] `_retryWithBackoff` unreachable 加 noqa B008 + 注释
- [x] ruff 零警告
- [x] Milvus round-trip 7/7 × 3 run 全绿
- [x] 相关 unit 测试 21/21 通过
- [x] health 200×2
- [x] MEMORY + 评估文档同步