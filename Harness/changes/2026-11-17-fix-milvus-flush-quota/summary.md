---
created: 2026-11-17
updated: 2026-11-17
sources: [生产日志(ontology_service ERROR/WARNING), milvus v2.4.6 容器内 milvus.yaml, pymilvus 3.0.1 decorators.py, PR #143 / 584bc361, Milvus 官方文档 configure_quotaandlimits]
tags: [Milvus, 向量, flush, 限流, 本体, 生产事故]
---

# Milvus 集合级 flush 配额打满 ⇒ 本体属性向量同步硬失败

> **状态**：根因已定位（Phase 1/2 证据闭环）；**立即止血 = 生产 Milvus 配置调整**（用户执行，见
> `deploy-prod-milvus-config.md`）；**代码加固 = 本分支 `fix/milvus-flush-quota`**（导入路径批量收口 +
> 集合级写入串行化 + 同步入口异步化）。实施记录见 §7。

## 1. 现象

生产后端日志（2026-11-17）：

```
ERROR:app.services.ontology_service:Milvus embedding 同步失败 id=7707:
<MilvusException: (code=8, message=[flush] Retry run out of 75 retry times,
 message=request is rejected by grpc RateLimiter middleware, please retry later:
 rate limit exceeded[rate=0.1])>

WARNING:app.services.ontology_service:Milvus 属性向量自动同步失败 id=7707 name=creator_name:
向量同步失败: <MilvusException: ... rate limit exceeded[rate=0.1]>
```

- 报错点：`backend/app/services/ontology_service.py:1861`（`syncEmbedding` 的 except）
  与 `:2673`（`_syncPropertyEmbeddingNow` 的 except）。
- 该实体 id=7707 在本地库不存在（本地 `ontology_property` 最大 id=4186、`ontology_class` 最大 id=77）
  ⇒ **生产日志**，非本地。
- **CRUD 已成功返回 200**，只有后台向量同步失败 ⇒ 用户无感，但**该属性的语义召回失效**。

## 2. 根因链（四层，逐层都有实测证据）

### 2.1 服务端：Milvus 集合级 flush 配额 = 0.1 qps（每 10 秒 1 次）

本地 `qa-milvus` 容器（`milvusdb/milvus:v2.4.6`）内 `/milvus/configs/milvus.yaml:649-655`：

```yaml
  flushRate:
    enabled: true
    max: -1 # qps, default no limit, rate for flush
    collection:
      max: 0.1 # qps, default no limit, rate for flush at collection level.
    db:
      max: -1 # qps of db level, default no limit, rate for flush
```

- 本地 compose **未挂任何配置覆盖**（`docker/docker-compose.yml` 的 milvus 服务只有 `milvus_data` 卷）
  ⇒ 这是**镜像出厂值**。
- Milvus 官方文档 `configure_quotaandlimits` 与之一致：`flushRate.collection.max` 默认 **0.1 QPS**，
  全局 `max` 与 db 级默认 `-1`（不限）⇒ **限的是「每集合」**。
- 报错文案 `rate limit exceeded[rate=0.1]` 与 `rate=0.1` 精确对上 ⇒ **就是这条配额**。

### 2.2 客户端：单实体在线写路径对同一集合发 **2 次 flush**

`ontology_service.syncEmbedding`（v1 集合）：

```python
milvus.deleteByOntologyIdDual(ontologyId, type)   # delete + flush   ← milvus_dual_write.py:227
milvus.insertEmbeddingsDual([{...}])              # insert + flush   ← milvus_dual_write.py:195
```

⇒ **同一个集合上连续 2 次 flush**，间隔必然 < 10s ⇒ **第 2 次必然被服务端拒绝**。

⭐ **这解释了仓库里长期存在的「单次 flush 10-25s / 8-25s」注释**（`ontology_service.py:201,2509,2569`、
`local_import_service.py:442` 等）：那不是 Milvus 慢，**是我们在吃自己的限流税**——pymilvus 重试到
配额窗口重新打开（≈10s）才成功。

### 2.3 扇出：批量导入时 N 个属性 ⇒ N 个并发同步任务 ⇒ 2N 次 flush 抢一个「10 秒 1 次」的桶

- `_vectorSyncLock(type_, entityId)` 只锁**同一实体**（`ontology_service.py:219-260`）⇒ 不同属性**完全并行**。
- `_PENDING_SYNC_TASKS` + `asyncio.create_task` ⇒ 批量导入 N 个属性 ⇒ N 个后台任务。
- **缺口（真根因）**：`createClass` 有 `sync_embedding: bool = True` 开关，导入向导用它**抑制逐类同步**、
  循环结束后改走 `syncMissingClassEmbeddingsBestEffort()` 整批补齐（单次 flush）
  （`local_import_service.py:442-448, 355-357`）；
  但 **`createProperty` 没有任何抑制开关** ⇒ `local_import_service.py:482` 逐条建属性 ⇒
  **每条属性各起一个后台同步任务** ⇒ 扇出。
- 令牌桶算术：pymilvus 的 `flush` 装饰器是 `retry_on_rpc_failure(retry_times=75, initial_back_off=0.01,
  max_back_off=3, back_off_multiplier=3)` ⇒ 75 次重试 ≈ **210s**；期间该集合只放行 **21 个 flush 令牌**
  （0.1 qps）⇒ **2N > 21（N ≳ 11）就必然有请求耗尽重试 ⇒ 硬失败**。

### 2.4 热修把「慢但对」变成了「报错且丢向量」—— **不是热修的 bug，是热修暴露了旧设计的额度假设**

| | 改前（`10c79bb3`） | 改后（`584bc361` ⇒ PR #143 ⇒ main `86ff4a1f`） |
|---|---|---|
| Milvus 调用位置 | `self.syncEmbedding(...)` **直接同步调用**（阻塞事件循环） | `await asyncio.to_thread(self.syncEmbedding, ...)` |
| flush 并发度 | **1**（事件循环被阻塞 ⇒ 后台任务天然串行） | **N**（真并发） |
| 与 0.1 qps 配额 | 间隔 ≥ 10s ⇒ **永远合规**，代价是每个属性把整个后端卡 10-25s | 2N 次 flush 抢桶 ⇒ **N ≳ 11 即硬失败** |

⇒ 热修本身是对的（它修的正是「整个后端被每个属性阻塞 10-25s」这个更严重的问题），
但它把**写路径与 Milvus flush 配额不匹配**这个一直存在的设计缺陷从「慢」变成了「错」。

## 3. 影响面：**同步硬失败**（不是延迟）；「旧向量还在不在」不能凭日志断言

`syncEmbedding` 的顺序是**先删后插**，两次 flush 各自带一次。报错只说 `[flush]` 重试耗尽，
**没说是哪一次 flush 失败**，于是有两种可能：

| 失败的 flush | 当时的库内状态 | 后果 |
|---|---|---|
| **delete 的 flush** | delete RPC 已成功（delete 任务已被服务端记录），insert **从未发出** | 旧向量在 delete 落地后消失、新向量从未写入 ⇒ **向量丢失** |
| **insert 的 flush** | delete 的 flush 已成功；insert RPC 已成功，行在 growing segment（可检索、未固化） | 旧向量已被删、新行已存在 ⇒ **向量多半还在**，只是未固化 |

⇒ **能确定的是：该实体的向量同步失败了**（CRUD 仍返回 200，用户无感 ⇒ 语义召回可能拿到旧向量或查不到）。
⇒ **不能确定的是「向量已经彻底消失」** —— Milvus 的 delete 可见性在本仓库早有记录：
delete 后要 **release + load** 才在 QueryNode 可见（见 `milvus_client.refreshCollection` 的注释、
`test_backfill_milvus_delete_visibility.py`）⇒ 单看一条 ERROR 日志无法判定旧行是否还在。

⇒ 处置动作不受影响：**以 PG 为真源对账补齐**。仓库已有现成入口（admin）：
`POST /api/v1/ontology/embeddings/sync-missing` ⇒ `syncMissingClassEmbeddings`
（补齐**缺失**的类/属性/指标向量，整批 insert ⇒ **单次 flush**）；
它的响应里的 `missingPropertyCount` / `syncedPropertyCount` 同时就是**这次事故的真实影响面**。

## 4. 修复方案（三件套，分层）

| 层 | 措施 | 归属 | 何时 |
|---|---|---|---|
| **A. 止血** | 生产 Milvus 挂 `configs/user.yaml` 覆盖 `quotaAndLimits.flushRate.collection.max: 0.1 → 5` + 重启 Milvus | **用户执行**（生产数据面变更） | **立即**（见 `deploy-prod-milvus-config.md`） |
| **B. 修复** | 跑一次 `POST /api/v1/ontology/embeddings/sync-missing` 补回丢失的向量 | **用户执行** | A 之后 |
| **C. 加固** | 代码：① 导入向导对属性抑制逐条同步（整批补齐，单次 flush）；② **集合级写入串行化**（同集合的「删+插+flush」排队 ⇒ 令牌不被兄弟请求抢走 ⇒ pymilvus 的重试预算必够）；③ 同步入口异步化（Milvus 同步 SDK 移出事件循环）；④ 集合名 SSOT（`milvus_collections`）+ 写顺序守卫测试 | **本分支（开发机）** | 下一批发版 |

### 为什么 A 是「立即止血」而不是「可选优化」

- 0.1 qps ⇒ 该集合**每分钟只放行 6 次 flush**。任何「逐实体写 + 每次 flush」的设计在数学上
  只能支撑 **3 个实体/分钟**。客户端再怎么排队也变不出吞吐 ⇒ **要么批量，要么抬配额**。
- 抬配额后：单次 flush 回到毫秒级 ⇒ 仓库里「flush 要 10-25s，所以必须丢后台、必须批量化」
  这一整套复杂度的前提消失（现有代码不用动也不会慢）。
- 官方文档同时提醒：flush 过密会产生大量小 sealed segment ⇒ compaction 开销上升。
  本系统写入量很小（百量级实体/天），5 qps 远低于 compaction 的压力阈值。

## 5. 明确不做的事

- ❌ **不改** `syncEmbedding` 的「先删后插」语义（删可见性依赖 Milvus 段固化/release+load，
  动它需要真实 Milvus 行为验证，而本地 Milvus 是生产数据面 ⇒ 禁连测试）。
- ❌ **不把「删 + 插」的 2 次 flush 合并成 1 次**：Milvus **不保证同一次 flush 里
  delete 与 insert 的先后** ⇒ 合并后 delete 表达式（`ontology_id == X`）有可能把
  **同一次 flush 里刚插入的那一行**一起删掉 ⇒ 向量**静默消失**（比现在更难查）。
  这条顺序语义**无法在本仓库实测**（本地 Milvus 是生产数据面、禁写测试）⇒ 不做
  无法验证的语义改动；改用「抬服务端配额」把两次 flush 的间隔拉开
  （A：0.1 → 5 qps ⇒ 两次 flush 相差约 200ms）。
- ❌ **不删**在线路径的 flush：flush 只保证持久化，检索可见性由查询一致性保证；
  删掉会让「崩溃后未固化数据丢失」的面变大。
- ❌ **不在生产跑** `backend/scripts/ontology_background_sync_realdata.py`（会删数据，仅测试库）。
- ❌ 本批**零 DDL、零生产写**（配置改动与 sync-missing 由用户在生产执行并留痕）。

## 6. 证据索引

| 事实 | 取证方式 |
|---|---|
| 集合级 flush 配额 0.1 qps | `docker exec qa-milvus grep -A6 flushRate /milvus/configs/milvus.yaml`（本地 v2.4.6，无覆盖卷） |
| 错误来自服务端限流 | 报错文案 `rate limit exceeded[rate=0.1]` + 官方文档同款文案 |
| pymilvus 重试预算 75 次 ≈ 210s | `pymilvus/decorators.py:322` `retry_on_rpc_failure(retry_times=75, initial_back_off=0.01, max_back_off=3, back_off_multiplier=3)` |
| 单实体 2 次 flush | `milvus_dual_write.py:195`（insert+flush）、`:227`（delete+flush） |
| 导入向导只抑制了类、没抑制属性 | `local_import_service.py:442-448`（类，`sync_embedding=False`）vs `:482`（属性，无开关） |
| 改前 flush 阻塞事件循环 ⇒ 天然串行 | `10c79bb3:backend/app/services/ontology_service.py:2381` 直接 `self.syncEmbedding(...)` |
| 改后真并发 | `584bc361` diff：`await asyncio.to_thread(self.syncEmbedding, ...)` |
| 本地无 id=7707 ⇒ 生产日志 | 本地 `ontology_property` max id=4186 / `ontology_class` max id=77 |

## 7. 实施记录（代码加固 C，分支 `fix/milvus-flush-quota`）

### 7.1 变更清单

| 文件 | 变更 | 为什么 |
|---|---|---|
| `backend/app/infrastructure/milvus_collections.py`（新） | 4 个集合名 + `embeddingCollectionForType` 的**唯一来源** | 写入锁按集合名取键 ⇒ 映射必须单点，否则「锁错集合」 |
| `backend/app/infrastructure/milvus_write_lock.py`（新） | 集合级 `asyncio.Lock`（`milvusWriteLock` / `milvusWriteLocks`）+ **等锁上限** `WRITE_LOCK_TIMEOUT_SECONDS = 30` | 同集合写入串行 ⇒ 令牌不被抢；**有上限才不会把「有界失败」变成「无限挂起」** |
| `backend/app/infrastructure/milvus_client.py` / `milvus_search.py` | 集合名常量改为引用 `milvus_collections`（行为零变化） | 读侧也并入 SSOT：读写集合名漂移 ⇒ 向量「写成功但召回不到」 |
| `backend/app/infrastructure/milvus_dual_write.py` | 同上 | 消除字面量副本 |
| `backend/app/infrastructure/milvus_query_helpers.py` | 同上 | 同上 |
| `backend/app/services/ontology_service.py` | 新增 `syncEmbeddingAsync`（取集合锁 + `to_thread`，等锁超时转 `OntologyError`）；4 处调用点改走它；批量补齐与删除路径持集合锁；批量补齐**在锁内重判存在性**；属性对账**过滤墓碑父类**；`createProperty` 新增 `sync_embedding` 开关 | 串行化 + 移出事件循环 + 抑制逐条扇出 + 消除 TOCTOU 重复向量 + 防墓碑向量复活 |
| `backend/app/services/kpi_vector_graph_sync.py` | 自维护的全局锁改为**同一把集合级锁** | 同一集合上有两把互不相识的锁 ⇒ 走 `syncEmbeddingAsync(type="kpi")` 的调用方会与本模块并发 flush ⇒ 互斥失效 |
| `backend/app/services/local_import_service.py` | 导入路径属性传 `sync_embedding=False`；批量补齐闸门放宽到「类**或**属性」 | 去掉 N 个并发 flush 的扇出，且属性仍能拿到向量 |
| `backend/app/api/v1/ontology.py` | 单条同步端点改走 `syncEmbeddingAsync` | 同上 |
| `backend/app/services/knowledge_compiler_service.py` | 编译器的属性/指标同步改走 `syncEmbeddingAsync` | 原先是**直接同步调用**（阻塞事件循环） |

### 7.2 测试证据（定向，均为真实 PostgreSQL + fake double）

| 文件 | 用例数 | 钉住的行为 |
|---|---|---|
| `app/tests/unit/test_milvus_write_lock.py`（新） | 8 | 同集合串行 / 跨集合并行 / 多集合不死锁 / 每 loop 独立 / **等锁超时** / 超时后锁状态不被污染 / 默认上限有界 / 多集合锁也受上限约束 |
| `app/tests/unit/test_milvus_write_order_guard.py`（新） | 6 | delete→insert 两次 flush 的**顺序不可合并**；集合名**五处**同源 |
| `app/tests/unit/test_local_import_vector_batch.py`（新） | 3 | 导入抑制逐属性同步；属性也走批量补齐；`sync_embeddings=False` 不批量 |
| `app/tests/unit/test_ontology_vector_flush_quota.py`（新） | 7 | 写路径步骤顺序；同集合串行 / 跨集合并行；`createProperty` 抑制开关；**对账锁内重判**（不重复写行）；**墓碑类属性不进对账** |

- 邻近套件回归：本体/导入/编译器/KPI 向量/Milvus 读写等 **28+ 个 unit 文件全绿**
  （`test_kpi_vector_graph_sync` / `test_local_import_service` / `test_milvus_client` /
  `test_milvus_kpi_routing` / `test_milvus_3collection_fields` / `test_milvus_optional_collection` /
  `test_milvus_write_target_guard` 等）。
- 集成回归：`test_ontology_reconcile_sync.py` + `test_local_import.py` ⇒
  **11 失败 = 与基线（`86ff4a1f`，独立库 `qa_metadata_test_base2`）逐条相同**
  （根因：`seedHeaderOntologyClasses` 让 `totalClasses` 期望值 2 变成 7 —— 与本次改动无关，
  属既有测试口径过期）⇒ **净新增失败 0**。

> 测试库说明：共享的 `qa_metadata_test`（5434）当时停在 **0151**（别的分支的迁移），本分支
> 只到 **0148** ⇒ 夹具的 `alembic upgrade head` 报 `Can't locate revision '0151'`。
> 本批全部用**独立临时库** `qa_metadata_test_flush` / `qa_metadata_test_flush2` /
> `qa_metadata_test_base2`（同实例 5434）跑，跑完已删。

### 7.3 发版后的验收口径

1. 批量导入一个含 N 个属性的表 ⇒ 日志里**每集合的 flush 次数 ≤ 1**（批量路径单次 flush）。
2. 同一属性连续编辑 3 次 ⇒ 日志无 `rate limit exceeded`。
3. `POST /api/v1/ontology/embeddings/sync-missing` 跑两次 ⇒ 第二次
   `missingPropertyCount = 0`。

### 7.4 评审提出、本批**明确不做**的项（留档，避免被当遗漏）

安全/代码双评审（`security-reviewer` 完整；`code-reviewer` 因模型配额 429 未出结论，
其视角由安全评审覆盖）提出以下项，**逐条记录不做的理由**：

| 项 | 内容 | 为什么不在这批做 |
|---|---|---|
| H2 | DB session 在持有 Milvus 写锁期间仍然打开（连接池占用时间 = 持锁时间） | 锁等待已有 30s 上限 ⇒ 最坏占用有界；缩短需要把 embedding 生成移出 session 事务边界，属结构性改动 |
| H3 | 「导入即触发全局对账」没有闸门（任何人导入都触发一次全量对账） | 这是**产品决策**（谁能触发、频率多少），按 [[qa-system-governance-write-gate]] 的写面收口另行裁定 |
| M2 | `syncRegistryV2Embeddings` 仍是逐条 delete+flush | 整段受 `ENABLE_SEMANTIC_REGISTRY_RAG` 开关控制、生产默认**关** ⇒ 当前不产生 flush 扇出；开启前须先批量 |
| M4 / M5 / M6、L1–L4 | 日志措辞、异常类型细化、常量命名等 | 不影响正确性；随下次触碰该文件时顺带 |

相关：[[qa-system-prod-hotfix-1116-vecsync]]（PR #143 = 2.4 里的热修）、
[[qa-system-bg-task-session-isolation]]（热修的根因与锁约定）、
[[qa-system-milvus-family]]（Milvus 家族其他坑）
