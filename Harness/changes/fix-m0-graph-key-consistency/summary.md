# 变更：fix-m0-graph-key-consistency

- **日期**：2026-10-03
- **作者**：Claude / 启琳
- **状态**：已实现 + 回归绿色（待提交）
- **来源**：`Harness/changes/fix-neo4j-test-isolation/summary.md` 的 Task 3 + Task 4
  （Task 4 实施时又发现同族第 3 处，一并纳入）

---

## 1. 根因：`022fad3 feat(id-mapping): M0-P0.1 unified_id 写路径改造` 只改了写侧

`neo4j_client.py` 现在是**半迁移**状态（202–430 行用 `unified_id`，455–561 行仍用 `id`）：

| # | 位置 | 现状 | 后果 |
|---|---|---|---|
| A | `getNodeRelationships:559` | `MATCH (n:{label} {id: $id})` + `related.id AS targetId` | **恒返回 `[]`**（HTTP 200 静默空） |
| B | `api/v1/graph.py::_stripNode` | 白名单不含 `unified_id` | 列表端点**不下发主键**，前端无法标识节点 |
| C | `syncOntologyNodes:468/477/487/495` | `MERGE (c:Class {id: r.id})` | 写出一批 **`id` 键的重复节点**（与 `unified_id` 那批并存） |
| D | `ontology_service.syncMissingGraph` | `joinPairs` 装 PG `int`、`existingJoinPairs` 是 unified_id `str` | 差集恒等于全集 ⇒ JOIN 对账**永不幂等**、`syncedJoinCount` 恒等于总数（假成功） |
| E | 同上（语义关系段） | `triple` 装 PG `int`、`existingRelTriples` 是 unified_id `str` | 同上（`SUPPLIES` 等语义关系同样永不幂等） |
| F | 同上 | `syncedRelations += 1` 出现在 `if` 内外**各一次** | 重复计数；uid 解析失败时也照加 |

A/B 是**生产可见**的：图浏览整块是死的，且以 200+空数组失败。
D/E/F 是**假成功**：接口报「补了 N 条」，实际每次重写全部边。

## 2. 真机证据（2026-10-03）

```
GET /api/v1/system/graph/nodes?label=Class            → 34 条，字段无主键
GET /api/v1/system/graph/nodes/Class/1/relationships  → []
POST /ontology/graph/sync-missing ×2                  → 两次都 missingJoin=57
PG 57 个类对 / Neo4j 仅 9 条 JOIN 边
```

## 3. 方案：统一以 `unified_id` 为准

M0 的全部意义就是 `unified_id` 成为跨存储稳定身份。在图边界回传 PG id 需要
反查（`id_mapping` 查询或解析 `obj:CLASS:9` 字符串格式），等于把 M0 刚拆掉的
耦合重新装回去 —— 故**改契约而不是加反查**。

### 后端

1. `getNodeRelationships(label, unifiedId: str)`：`MATCH (n:{label} {unified_id: $uid})`，
   返回 `related.unified_id AS targetUid`。
2. `api/v1/graph.py`：
   - `_stripNode` 白名单补 `unified_id` 并**改名为 `unifiedId`**（与全站 camelCase 契约一致）
   - 路径参数 `nodeId: int` → `unifiedId: str`
3. `syncOntologyNodes(classes, properties)`：入参行改带 `unifiedId`/`classUid`/`refClassUid`，
   Cypher 一律按 `unified_id` MERGE。调用方 `ontology_batch_service._syncGraph` 用
   `IdMappingService.listByBusinessObject` **批量**取映射（2 条查询，不是 3676 条）。
4. 删除 `getClassWithProperties`（全仓零调用者，且同属 `id` 键缺陷）。
5. `syncMissingGraph`：**先解析再差分** —— 把 join / relation 的 PG id 经 `id_mapping`
   转成 unified_id 之后再与 Neo4j 集合做差，两边同类型才有意义；顺带去重计数。

### 前端

`systemViewer.ts`：`GraphNode.id` → `unifiedId: string`、`GraphRelation.targetId` →
`targetUnifiedId: string`，路径参数 `encodeURIComponent`（`unified_id` 含 `:`）。
`Neo4jGraphPage.tsx`：ID 列 / rowKey / RelationPanel key 同步。

## 4. 全局约束

- **先出方案再改**（本文档）；TDD RED→GREEN
- 集成测试用真实 PG（`localhost:5434` / `qa_metadata_test`）+ 真实 Neo4j
  （`TEST_NEO4J_URI=bolt://localhost:7688`，见 `fix-neo4j-test-isolation`）+ **串行**
- **绝不手工跑 `alembic upgrade head`**
- 函数 < 50 行；camelCase；显式错误处理
- 改前端必须 `docker compose build --no-cache frontend`
- 禁裸 `git stash/pop`

## 5. 不在范围

- 不改 `getClassIds`/`getPropertyIds`/`getJoinPairs`/`getRelationTriples` 的
  `COALESCE(x.unified_id, toString(x.id))` 兼容读 —— 它是给存量旧节点兜底的，
  迁移期保留正确
- 不动 202–430 行已迁移的写路径

## 6. 风险

| 风险 | 缓解 |
|---|---|
| 契约破坏性变更（路径参数 int → str） | 消费者只有本仓 `Neo4jGraphPage`，已一并改；全仓 grep 确认无其他调用方 |
| 生产图里可能已有 C 产生的 `id` 键重复节点 | 修复后不再新增；存量清理另行评估（本变更不做删除，避免误删） |
| `_syncGraph` 批量取映射后行为变化 | 保持 fail-open 语义不变；映射缺失的行直接跳过（不再写出无键节点） |

---

## 7. 实施记录与证据（2026-10-03）

### 7.1 一个额外发现：`_testapp.py` 根本没挂 `graph.router`

集成测试的 app（`app/tests/_testapp.py`）没有 `include_router(graph.router, ...)`，
所以 `/system/graph/nodes*` **零集成覆盖** —— 这才是 A/B 两个静默缺陷能长期存活的
直接原因：没有任何测试会走到它们。

顺带发现 `app/tests/integration/test_route_auth_guard.py`（本应守住这类装配/鉴权
缺口的守卫）**当前是坏的**：`from fastapi.routing import _IncludedRouter` 在现装
FastAPI 版本已不存在 ⇒ **整个 integration 套件收集即中断**，必须 `--ignore` 才能跑。
修它需要摸清 FastAPI 新版路由内部结构，**不在本变更范围，另行处理**。

**建议的收尾守卫**（本变更未做）：`test_app_wiring.py` 目前只断言「catch-all mount
之后不得有路由」+「health 可达」，不比对 `main.py` 与 `_testapp.py` 的**路由集合**。
补一条 parity 断言（`main.py` 里挂的每条路径都能在 `_testapp` 上解析到）即可让这类
「加了 main 忘了 testapp」的漏挂当场变红 —— 前提是先修好 `test_route_auth_guard.py`
的路由枚举（两者共用同一套展开逻辑）。

### 7.2 测试侧的关键修正：假替身把错误前提钉死了

`test_ontology_reconcile_sync.py::_FakeNeo4j` 声明
`getJoinPairs() -> set[tuple[int,int]]`、`getRelationTriples() -> set[tuple[int,int,str]]`，
而生产返回 **unified_id 字符串**。替身照着「两边都是 PG id」这个**错误前提**写，
于是 int/str 差集的真实缺陷在测试里永远看不见。

同类：`upsertClassNode` 用 `*args` 记录，而生产按**关键字**调用 ⇒ 记成空元组，
断言 `c[1][0]` 直接 IndexError。已改为按**真签名**记录（顺带成为参数名契约检查）。

> 这两条是「假替身保真度」的又一次复现：替身必须复刻生产的**类型与调用形状**，
> 否则它保护的是替身自己，不是生产。

### 7.3 验证矩阵

| 项 | 结果 |
|---|---|
| `test_ontology_relation_graph_integration.py` | 4 条既有红 → **全绿**；新增 2 条端点契约测试（列表下发 `unifiedId`、关系端点按 encode 后的 unified_id 定位） |
| `test_ontology_reconcile_sync.py` | 既有 2 条红 → **12 passed**（幂等性成为真断言） |
| `test_ontology_batch_api.py` | 22 passed；新增 `test_sync_graph_keys_nodes_by_unified_id` 断言无 `id` 键重影节点 |
| 断言敏感性 | 在测试实例上手工 `MERGE (c:Class {id: 999999})` ⇒ 计数器 1，删后 0 —— 证明该断言确实对旧写法敏感（不靠「绿」自证） |
| 前端 | `systemViewerApi` / `Neo4jGraphPage` 新增用例 RED → GREEN；`npm run build`（含 tsc）通过 |

### 7.4 未做

- `getClassWithProperties` 零调用者，**迁移到 unified_id 但未删除**（删除属独立清理，
  与「改键」混在一起会让 diff 难审）
- 存量 `id` 键重影节点不做清理
- `test_route_auth_guard.py` 的 FastAPI `_IncludedRouter` 破损不修
