# 变更：fix-neo4j-test-isolation

- **日期**：2026-10-03
- **作者**：Claude / 启琳
- **状态**：Task 1 + Task 2 已落地并真机验证；Task 3 / Task 4 为独立缺陷，待排期
- **事故**：2026-10-03 跑 `integration/test_class_restore.py` 时**清空了生产 Neo4j
  本体图谱**（`:Class` 64 → 0、`:Property` 全没、`:Metric` 全没）。已用
  「补图信息」（`POST /ontology/graph/sync-missing`）从 PG 重建（34 类 / 3642 属性 /
  3179 HAS_PROPERTY 边），重建过程 0 失败、幂等（二次跑 missingClass=0）。

## 0. 落地结果（2026-10-03）

| 项 | 证据 |
|---|---|
| 守卫单测 | `unit/test_neo4j_test_uri_guard.py` **8 passed**（含双向：坏输入被拦 + 正确输入不误拦） |
| fail-fast 生效 | 不设 `TEST_NEO4J_URI` 跑 integration ⇒ 8 errors（会话级夹具抛出，消息含起容器命令）；unit 不受影响（8 passed） |
| 独立实例 | `qa-neo4j-test`（`neo4j:5.23-community`，7688/7475），与生产同镜像 tag |
| 真机跑通 | `test_class_restore.py` **8 passed** 打独立实例；跑完该实例节点数 = 0（清理落在测试库） |
| **生产零损伤** | 跑前跑后 `qa-neo4j` 均为 **Class 34 / Property 3642 / JOIN 9** —— 逐字相同 |
| 零新增红 | `test_ontology_relation_graph_integration.py` 4 red：HEAD 基线 worktree 同款 4 red（见 §3） |

---

## 1. 根因：三环链

| 环节 | 事实 |
|---|---|
| ① 默认值 | `app/config.py:37` `neo4jUri: str = Field(default="bolt://localhost:7687", alias="NEO4J_URI")` |
| ② 端口暴露 | `docker/docker-compose.yml:38` `neo4j` 服务把 `7687` 发布到 `0.0.0.0` —— **就是生产实例** |
| ③ 无隔离 | 宿主机跑 pytest 不设 `NEO4J_URI` ⇒ 用默认值 ⇒ 直连生产；而 `tests/integration/conftest.py:74` 的 teardown 执行<br>`MATCH (n) WHERE n:Class OR n:Property OR n:Metric DETACH DELETE n` |

⇒ **任何用到 `neo4jCleanDriver` 的集成测试，跑一次清一次生产图谱。**
`test_ontology_relation_graph_integration.py:32` 还有一处同样的裸 `DETACH DELETE`。

**这是 Milvus 事故的同形复发**：`milvusCleanClient` 曾 drop 真实集合，当时用
`MILVUS_DB_NAME=qa_test` 隔离修掉（见记忆 `qa-system-milvus-test-drops-prod`）；
Neo4j 从未做等价隔离。

对照 PG：`_pg_support.resolveTestDatabaseUrl()` **未配置 `TEST_DATABASE_URL` 就
fail-fast**，绝不静默连生产。Neo4j 这条路径**完全没有这层守卫** —— 这才是本质缺陷：
不是「忘了配测试实例」，而是**没有 fail-fast，所以忘配也不会被发现**。

---

## 2. 方案

### Task 1（必做）：fail-fast 守卫 —— 不配测试实例就拒绝跑 ✅

新增 `backend/app/tests/_neo4j_support.py`，三个函数（与 `_pg_support.resolveTestDatabaseUrl`
同形，但**不接受 `NEO4J_URI` 兜底** —— 宿主机那个值通常就是生产）：

| 函数 | 职责 |
|---|---|
| `resolveTestNeo4jUri(*, uri=None)` | 未配 `TEST_NEO4J_URI` → 抛错；配成 7687 → 也抛错 |
| `installTestNeo4jEnv()` | 把 `NEO4J_URI` **覆盖**为测试实例 + 清 settings 缓存 + `closeDriver()` |
| `assertAppNeo4jIsIsolated(*, appUri=None)` | 破坏性清理前复查「应用侧 == 测试实例」，抓配置漂移 |

**为什么不止守夹具**：`test_ontology_relation_graph_integration.py` 走的是应用的
`neo4j.getDriver()`（读 `settings.neo4jUri`），与夹具的 URI 各算各的。只守夹具会得到
**半隔离** —— 夹具连测试实例、被测代码连生产，破坏照样落在线上却看不出。
`installTestNeo4jEnv()` 让两条路径共用一个事实源。

**为什么放会话级 fixture 而不是 conftest 导入期**：导入期抛错会让 `pytest app/tests`
变成**整体收集错误**，连 unit 都跑不了。会话级 autouse fixture 只让 integration 红
（`pytest app/tests/unit` 不加载该 conftest）—— 爆炸半径正好，消息里带起容器命令。

**TDD**（`unit/test_neo4j_test_uri_guard.py`，8 例，双向）：
| 用例 | 断言 |
|---|---|
| 未设 `TEST_NEO4J_URI` | 抛 `RuntimeError`，消息含「生产」 |
| `TEST_NEO4J_URI` = 7687 | 抛错（事故的形态） |
| `TEST_NEO4J_URI` = 7688 | 原样返回（不误拦） |
| 只设 `NEO4J_URI` = 生产 | 抛错，**绝不回落到 config 默认** |
| `install` 遇已存在的生产 `NEO4J_URI` | 覆盖为测试实例 |
| `install` 未配 `TEST_NEO4J_URI` | 抛错且**不动** `NEO4J_URI`（半配置更危险） |
| 应用侧漂移成生产 | `assertAppNeo4jIsIsolated` 拦下 |
| 两侧一致 | 放行 |

接线：`integration/conftest.py` 的 `isolateNeo4jEnv`（session autouse）+ `neo4jCleanDriver`
（连接用 `resolveTestNeo4jUri()`、teardown 前断言）；
`test_ontology_relation_graph_integration.py` 的 `_wipeOntologyNodes` 清空前三处断言。

### Task 2（必做）：独立 Neo4j 测试实例 ✅

Neo4j **Community 不支持多数据库**（`MILVUS_DB_NAME` 那招不适用），故按 `qa-pg-a1`
先例单起容器：

```bash
docker run -d --name qa-neo4j-test \
  -p 7688:7687 -p 7475:7474 \
  -e NEO4J_AUTH=neo4j/<TEST_PASSWORD> \
  -e NEO4J_PLUGINS='["apoc"]' \
  neo4j:5.23-community
```

用法（与 PG 同形）：
```bash
TEST_NEO4J_URI=bolt://localhost:7688 \
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:<pw>@localhost:5434/qa_metadata_test \
  python -m pytest app/tests/integration
```

不写进主 `docker-compose.yml`（`qa-pg-a1` 也不在里面）：测试实例是**宿主机侧
按需起**的，进 compose 会让 `compose up` 多拉一个生产用不到的库。

### Task 3（选做，独立缺陷）：`syncMissingGraph` 的 JOIN 对账类型不匹配

验证重建时发现，与本次隔离无关但同属图谱对账：

```python
joinPairs = {(j.source_class_id, j.target_class_id) for j in joins}   # set[(int, int)]
missingJoinPairs = joinPairs - existingJoinPairs                       # existingJoinPairs 是 set[(str, str)]
```

`existingJoinPairs` 来自 `neo4j.getJoinPairs()`，返回的是 **`unified_id` 字符串**
（`"obj:CLASS:9"`），而 `joinPairs` 装的是 **PG bigint id**。整数永不等于字符串 ⇒
`missingJoinPairs` **恒等于全部 joinPairs** ⇒

- JOIN 对账**永不幂等**：每次跑都重写全部边（MERGE 兜住，不会重复建）
- `syncedJoinCount = len(missingJoinPairs) - failedJoins` **恒等于总数** ⇒ 假成功
- 线上实测：PG 57 个类对，Neo4j 只有 **9** 条 `JOIN` 边；再跑一次仍报 `missingJoin=57`

修法：比较前把 PG id 经 `id_mapping` 转成 `unified_id` 再 diff（或在循环内记录
实际写入的 pair 集合用于回读）。**建议单独开一个变更**，别混进隔离修复。

> 附注（待查，非本方案承诺）：57 个类对只落地 9 条边，可能还有第二层原因
> （`linkClassJoin` 的端点解析失败会记 failures，但本次 `failedCount=0`）。
> 需要在 Task 3 里一并定位。

### Task 4（独立缺陷，**生产可见**）：图浏览端点自 M0 起静默失效

本次隔离验证时暴露（4 个集成 red 的真因，**非隔离改动引入**）。

`commit 022fad3 feat(id-mapping): M0-P0.1 unified_id 写路径改造` 把节点主键从 `id`
改成 `unified_id`，但读写两侧只改了一半：

| 位置 | 现状 | 后果 |
|---|---|---|
| `neo4j_client.upsertClassNode` | `MERGE (c:Class {unified_id: $unified_id})` | 节点只有 `unified_id`，无 `id` |
| `neo4j_client.getNodeRelationships:559` | `MATCH (n:{label} {{id: $id}})` | **永不匹配** ⇒ 恒返回 `[]` |
| `api/v1/graph.py` `_stripNode` | `known` 白名单**不含 `unified_id`** | 前端拿到的节点没有主键 |

真机实测（生产 :5173）：

```
GET /api/v1/system/graph/nodes?label=Class      → 34 条，但字段只有 sourceTable/name/alias/description（无 id）
GET /api/v1/system/graph/nodes/Class/1/relationships → []      # HTTP 200，静默空
```

即：**图浏览功能整块是死的**，且以 HTTP 200 + 空数组的形式失败（无报错、无日志），
前端只会以为「这个节点没有关联」。

修法（建议单独变更）：统一以 `unified_id` 为准 —— 列表端点回传 `unified_id`，
关系端点把路径参数改为 `unified_id: str`（或在服务端用 `id_mapping` 把 PG id 转过去），
`_stripNode` 白名单补 `unified_id`。注意 `getNodeRelationships` 的返回里
`related.id AS targetId` 同样是死引用，要一并改成 `related.unified_id`。

> 相关：同族缺陷 Task 3（JOIN 对账 int/str 类型不匹配）。两处都是「M0 统一 ID 只改了
> 一侧」，建议合并成一个「M0 读写一致性收尾」变更做，别零散修。

---

## 3. 全局约束

- **先出方案再改**（本文档即方案）；TDD RED→GREEN
- 测试用真实 PostgreSQL（`localhost:5434` / `qa_metadata_test`）+ **串行**
- **绝不手工跑 `alembic upgrade head`**
- 函数 < 50 行；camelCase；显式错误处理
- 禁裸 stash/pop；`git add` 显式路径

## 4. 不在范围

- 不改 Neo4j 生产实例的端口暴露（生产需要 7687）
- 不引入"测试自动建容器"的编排（与 `qa-pg-a1` 保持一致，人工起）
- 不在本变更修 `syncMissingGraph`（Task 3 另开）
- 不在本变更修图浏览端点（Task 4 另开；它是**生产功能缺陷**，不属于测试隔离）
- `test_ontology_relation_graph_integration.py` 的 4 red **保持红** —— 它们是 Task 4 的
  诚实信号，用 skip 掩盖等于把生产缺陷藏起来

## 5. 风险

| 风险 | 缓解 |
|---|---|
| 加守卫后**所有**用到 Neo4j 的集成测试立刻变红（没配 `TEST_NEO4J_URI`） | 这是**期望行为** —— 红比清库好。文档 + 本变更记录给出起容器与运行命令 |
| 有人图省事把 `TEST_NEO4J_URI` 指向生产 | 与 PG 同等风险，无法在代码层拦；但**至少需要显式配置**，不再是「什么都不做就连生产」 |
| 独立实例与生产版本漂移 | 固定同一镜像 tag `neo4j:5.23-community` |
