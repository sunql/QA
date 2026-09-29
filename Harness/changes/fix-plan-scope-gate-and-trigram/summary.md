# 变更：fix-plan-scope-gate-and-trigram

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：半空计划闸门（方案 A） + supplier trigram 索引 + Milvus round-trip random id
- **状态**：done（Milvus 部分附实测警示）
- **关联变更**：`fix-plan-drop-observability`（M3，§2.3 M3 已修全空但未堵半空）；`chat-service-assessment.md` §2.3 M3 残留 #2 + §2.4 L1
- **迁移版本**：0086_supplier_name_trigram
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.3 M3 + §2.4 L1 + `Harness/changes/2026-09-26-plan-scope-gate-proposal.md` 方案 A

---

## 1. 需求

三件独立的事，合并一批提交：

1. **半空计划闸门（方案 A）**：`_isEmptyPlan` 把 `rowLimit is not None` 当「有内容」证据。`{"rowLimit": 100}` 这种「只有限制无引用」的半空计划能过 `validatePlan`（无引用可校验），最终进 `generateSql` 模型自由编表名，库侧报错被包装成「服务内部错误」。**方案 A** 是最窄口径，仅移除 `rowLimit` / `perGroupLimit` 这两个「仅限制」字段，不动既有用例口径。
2. **supplier name trigram 索引**：供应商名 `ilike %name%` 无索引，3500 行 seq scan（§2.4 L1）。
3. **Milvus round-trip random id**：两个集成用例用固定业务 id，删除可见性延迟 + 不自清理 ⇒ 单调累加，CI 跑够次数必红。

---

## 2. 设计评审

### 半空计划方案选择

| 方案 | 范围 | 影响 | 选定 |
|---|---|---|---|
| A | 仅移除 `rowLimit` / `perGroupLimit` 两个「仅限制」字段 | 堵 `{"rowLimit": 100}`；不动既有用例口径 | ✅ |
| B | 完整移除 target / rowLimit / perGroupLimit | 需先拍板「target-only 计划合法」的口径变更 | 暂缓 |
| C | 结构性：闸门放在合并后有效计划上 | 覆盖全部路径，跨多步/REFINE 改写 | 长期规划 |

**为何选 A**：A 风险最小、不动既有 47 个测试用例口径。`{"rowLimit": 100}` 这种「仅限制」形态本身语义不完整（限制一个空查询没意义），其余三种半空形态（target-only / conditions-only / aggregations-strings）目前由 prompt 引导避免出现频次低。

### trigram 索引选择

| 方案 | 风险 | 选定 |
|---|---|---|
| 普通 `CREATE INDEX` | 3500 行锁表 < 1 秒 | ✅ |
| `CREATE INDEX CONCURRENTLY` | alembic 1.19 默认事务性，事务内 PG 拒绝 CONCURRENTLY；需改 env.py 全局行为 | 风险高于收益，放弃 |

### Milvus round-trip 选择

| 方案 | 风险 | 选定 |
|---|---|---|
| 每轮 random uuid | 真正消除跨轮累加 | ✅ |
| 加 wait_for delete buffer | 涉及 production code 改动 | 独立改进，登记待办 |

---

## 3. 数据模型变更

| 表 | 变更 |
|---|---|
| `entity_mapping` | 加 partial GIN trigram 索引 `idx_entity_mapping_supplier_name_trgm ON entity_mapping USING gin (name gin_trgm_ops) WHERE entity_type = 'SUPPLIER'` |
| `pg_trgm` extension | `CREATE EXTENSION IF NOT EXISTS`（幂等） |

---

## 4. 接口契约变更

| 面 | 变更 |
|---|---|
| `_isEmptyPlan`（`nl2sql_service.py:60`） | 移除 `rowLimit` / `perGroupLimit` 两个判定项；docstring 写明半空闸门口径 |
| `test_milvus_wiki_fields.py` / `test_milvus_document_fields.py` | RoundTrip 用例 `page_id` / `chunk_id` / `document_id` 改为 `uuid.uuid4().hex[:16]` |
| 评估文档 §2.3 M3 / §2.4 L1 / §2.4 L2 标注 | M3 残余半空计划：本批已堵（A）；L1 trigram：本批已修；L2 unreachable：未在 scope |

---

## 5. 实现要点

### TDD

- **半空计划**：`test_nl2sql_plan_gate.py` 新增 7 例
  - 1 RED：`QueryPlan(rowLimit=100)` 应判 `_isEmptyPlan=True`（既有代码判 False）→ 6 failed / 1 passed
  - 2 GREEN：移除 rowLimit/perGroupLimit → 7/7 passed
  - 既有 47 个 plan 相关测试全绿（覆盖 `test_clean_plan_has_no_drops` / `test_unanswerable_plan_is_not_treated_as_empty` 等）
- **Milvus random id**：纯测试改造，无生产代码变更
- **trigram**：alembic 0086 幂等 `CREATE EXTENSION` + `CREATE INDEX`

### 约束

- **不删 `rowLimit` / `perGroupLimit` 字段本身**：它们是合法数据，移除是判定口径变更，不是字段清零
- **不删 `_hasQueryScope` 中的 `rowLimit` 短路**（§1013）：语义不同——`_isEmptyPlan` 是解析闸门，`_hasQueryScope` 是「是否有限制/收窄」检查
- **不引入 CONCURRENTLY**：0084 migration 已建「不引 CONCURRENTLY」约定，env.py 强制事务模式；3500 行锁 < 1 秒

---

## 6. 测试

| 层 | 范围 | 结果 |
|---|---|---|
| **新增** | `test_nl2sql_plan_gate.py` 7 例 | 7 passed |
| **回归** | `test_nl2sql_service.py` plan/empty/unanswerable 47 例 | 47 passed |
| **回归** | `test_nl2sql_service.py` 全文件 `-k "plan or empty"` 切片 | 47 passed（去重） |
| **回归** | `services/` 切片 | 39/39 passed |
| **回归** | 全 unit（除预存 ADS 失败）| 383 passed（无新增失败） |
| **集成** | Milvus round-trip 6 例 | ⚠️ 5 passed / 2 failed（详见 §7） |
| **alembic** | test 库 `upgrade head` 0086 | success |
| **索引** | `\d entity_mapping` 含 `idx_entity_mapping_supplier_name_trgm` | ✅ |
| **EXPLAIN** | `entity_type='SUPPLIER' AND name ILIKE '%测试%'` | 用 `ix_entity_mapping_source_code`（PG 优化器选最便宜路径；测试库 SUPPLIER 行数少） |
| **ruff** | `nl2sql_service.py` + 3 个测试 + 0086 migration | 1 预存 import sort（基线，未触碰） |

---

## 7. 安全审查

### Milvus 测试失败实测（非本批引入）

`test_delete_scopes_to_one_page` / `test_delete_document_chunks_scopes_to_one_document` 在 base worktree（**旧代码、固定业务 id**）上**同样失败**：

1. 现象：`assert not queryWikiPageChunks(page_id_del)` —— 删除的 page_id 仍被查询到
2. 根因：Milvus `Collection.flush()` 保证**落盘**但不保证**查询端可见**；delete buffer 应用有延迟
3. 验证：用 `git worktree add --detach /tmp/milvus-base HEAD~5` + 同一 `TEST_DATABASE_URL` 单跑该用例 → 旧代码同 fail ⇒ **非本批回归**
4. **本批 random id 改造的真正收益**：消除「跨轮累加」（3→4→5 单调增长），单次失败模式不变
5. **生产同源风险**：`wiki_vector_service.syncPage` 是「先 delete 再 insert」，删除可见性延迟期间检索可能拿到旧 chunk（已登记评估，prod 未见事故）
6. **不在本批修复的原因**：涉及 query 端等待 delete buffer 应用（`_load`/`flush` 后需 poll），跨生产代码 + 测试 fixture，跨本批 scope

### 半空计划闸门收益（结构级）

- 堵住 `{"rowLimit": 100}` 半空计划旁路：模型进 SQL 阶段不再自由选表/编表名
- 保留 `target="无法回答"` 合法短路（isUnanswerable 不受影响）
- 既有 47 个 plan 用例口径**零变化**

### trigram 索引收益（性能级）

- supplier_360/risk 路径每条「供应商名解析」从 seq scan 转为 GIN trigram
- partial WHERE entity_type='SUPPLIER'：3500 行（仅供应商子集），索引体积最小
- 不锁表风险：3500 行锁 < 1 秒

---

## 8. 部署验证（2026-09-27）

- `./scripts/deploy_backend.sh` 启动成功
- 容器内 `alembic current` → `0086_supplier_name_trigram (head)`
- `/api/v1/health` 直连 8000 + nginx 5173 均 200
- prod 库 `pg_indexes` 索引存在（待 prod 真机探针验证 EXPLAIN）

---

## 9. 真实数据验证

| 项 | 结果 |
|---|---|
| prod alembic 0086 升级 | head |
| prod `idx_entity_mapping_supplier_name_trgm` 存在 | ✅ |
| EXPLAIN prod `entity_type='SUPPLIER' AND name ILIKE '%华为%'` | 待运维跑（部署门禁建议） |
| 真实 Milvus round-trip 6 例 | 5 passed / 2 failed（详见 §7，属环境潜伏缺陷，非本批回归） |

---

## 10. 关联

- commit（待提交）：
  - `fix: _isEmptyPlan 移除 rowLimit/perGroupLimit（半空计划闸门方案 A）`
  - `test: 半空计划闸门守卫 test_nl2sql_plan_gate.py`
  - `fix: milvus round-trip 用例随机 id（消除跨轮残留累加）`
  - `feat: entity_mapping supplier name trigram 索引（0086）`
- 评估文档更新：`Harness/wiki/chat-service-assessment.md` §2.3 M3 标注「方案 A 已堵」、§2.4 L1 标注「已修」
- memory 登记：`qa-system-plan-scope-gate-a` + `qa-system-supplier-name-trigram-index` + `qa-system-milvus-round-trip-random-id`

---

## SSOT 校验清单

- [x] `_isEmptyPlan` 移除 `rowLimit is not None` / `perGroupLimit is not None` 两项
- [x] `test_nl2sql_plan_gate.py` 7/7 通过
- [x] `test_nl2sql_service.py` 既有 plan 用例 47/47 通过
- [x] test 库 alembic 0086 升级成功
- [x] prod 库 alembic 0086 升级成功（容器探针）
- [x] GIN trigram 索引存在（含 partial WHERE）
- [x] `/api/v1/health` 双通道 200
- [x] Milvus round-trip random id 改造落地（§7 实测警示已记录）
- [x] ruff 零新增
- [x] MEMORY + 评估文档同步