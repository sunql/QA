---
created: 2026-11-17
updated: 2026-11-17
sources: [生产日志(ontology_service ERROR/WARNING), milvus v2.4.6 容器内 milvus.yaml, pymilvus 3.0.1 decorators.py, PR #143 / 584bc361, Milvus 官方文档 configure_quotaandlimits]
tags: [Milvus, 向量, flush, 限流, 本体, 生产事故]
---

# Milvus 集合级 flush 配额打满 ⇒ 本体属性向量同步硬失败（向量丢失）

> **状态**：根因已定位（Phase 1/2 证据闭环）；**立即止血 = 生产 Milvus 配置调整**（用户执行，见
> `deploy-prod-milvus-config.md`）；**代码加固 = 本分支 `fix/milvus-flush-quota`**（批量路径收口 + 客户端 flush 闸）。

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

## 3. 影响面：失败的不是「延迟」，是**向量丢失**

`syncEmbedding` 的顺序是**先删后插**：

- 若 **delete 的 flush** 被拒 ⇒ 旧向量**已删**（delete RPC 本身成功，失败的只是 flush）、
  新向量**从未插入** ⇒ **该属性的向量彻底消失**（语义召回彻底失效）。
- 若 **insert 的 flush** 被拒 ⇒ insert RPC 已成功、行在 growing segment 里（可检索但未固化）⇒ 影响较轻。

⇒ 需要一次「补缺失」的修复。仓库已有现成入口（admin）：
`POST /api/v1/ontology/embeddings/sync-missing` ⇒ `syncMissingClassEmbeddings`
（以 PG 为真源补齐**缺失**的类/属性/指标向量，整批 insert ⇒ **单次 flush**）。

## 4. 修复方案（三件套，分层）

| 层 | 措施 | 归属 | 何时 |
|---|---|---|---|
| **A. 止血** | 生产 Milvus 挂 `configs/user.yaml` 覆盖 `quotaAndLimits.flushRate.collection.max: 0.1 → 5` + 重启 Milvus | **用户执行**（生产数据面变更） | **立即**（见 `deploy-prod-milvus-config.md`） |
| **B. 修复** | 跑一次 `POST /api/v1/ontology/embeddings/sync-missing` 补回丢失的向量 | **用户执行** | A 之后 |
| **C. 加固** | 代码：① 导入向导对属性抑制逐条同步（整批补齐）；② 单实体写路径 2 次 flush 合并为 1 次；③ 客户端按集合的 flush 闸（串行 + 最小间隔）⇒ 把「服务端拒绝 + 重试耗尽」变成「客户端排队、最终必成功」 | **本分支（开发机）** | 下一批发版 |

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

相关：[[qa-system-prod-hotfix-1116-vecsync]]（PR #143 = 2.4 里的热修）、
[[qa-system-bg-task-session-isolation]]（热修的根因与锁约定）、
[[qa-system-milvus-family]]（Milvus 家族其他坑）
