# 变更：M7-1 知识服务 P95 压测脚本 + 实体查询全扫发现（设计稿 §11）

- 日期：2026-11-21　分支：`main`（工作区未提交）
- 判据：M7 判据 6「性能 P95」

## 一、新增脚本 `backend/scripts/bench_knowledge_p95.py`

零第三方依赖（urllib），串行单客户端，测**单请求延迟分布**（不是吞吐）。

三处"诚实口径"（都是踩过后加的，不是设计出来的）：

1. **限流**：知识接口 `30/minute`（`knowledge_facade.KNOWLEDGE_QUERY_LIMIT`）。
   第一版 20 连发 ⇒ 2 个用例 16/20 是 429，表里看起来"很快"。
   现：默认 `--delay 3.0`（20/分钟，留余量）、**预热也走间隔**（预热不计样本但**算配额**）、
   429 单独计数判**样本无效**（退出码 2），不计入延迟、也不算不达标。
2. **降级 ≠ 快**：`/knowledge/query` 的某个通道降级（如 embedding 服务不可用）
   仍返回 **200 且极快**。脚本校验 `routeResults[].status == DEGRADED`，
   有降级的样本判无效——否则"挂着的那条路"会被记成性能最好的。
3. **错误不混进延迟**：≥400 响应单独计数，不进分位数。

## 二、dev 站结果（数据规模见 `evidence/data-scale-dev.json`）

| 用例 | 样本 | P50 | P95 | 目标 | 结论 |
|---|---|---|---|---|---|
| 实体精确查询 | 20 | 0.348 | 0.362 | < 1.0s | ✅ |
| 图谱 1 跳 | 20 | 0.061 | 0.068 | < 2.0s | ✅ |
| 图谱 2 跳 | 20 | 0.088 | 0.094 | < 2.0s | ✅ |
| RAG 检索 | 20 | 0.080 | 0.087 | < 3.0s | ✅ |
| 文档检索 | 20 | 0.058 | 0.060 | < 3.0s | ✅ |
| 四通道综合 | 20 | 0.132 | 0.149 | 观测 | — |

规模参照（`evidence/data-scale-dev.json`）：Neo4j **19.26 万节点 / 52.3 万边**；
`entity_mapping` **35.4 万行**（SUPPLIER 3500 / 其余 ITEM）；Milvus v2 向量
**4265 条**（class 59 / property 4182 / kpi 12 / metric 12）+ 文档 32 chunk；
文档目录 4 份。

**压测方法学（吃了两次亏才定下来）**：
- 同一台机跑 `docker compose build` 期间测，实体查询 P95 从 0.36s 飙到 **28.3s**、
  文档检索 0.06→1.79s —— 这不是回归，是 I/O 争用。**压测必须在空闲机跑**，
  且只认「20/20 有效样本、无 429、无降级」的那一轮。

## 三、发现：实体查询走**全表扫**（生产超 1s 风险）

`EXPLAIN ANALYZE`（q 非数字、不带 entityType，即**前端当前调用口径**）：

```
Limit → Index Scan using entity_mapping_pkey
        Filter: enterprise_code ILIKE OR source_code ILIKE OR name ILIKE
        Rows Removed by Filter: 354421
Execution Time: 329 ms
```

根因：三个 ILIKE 用 `OR` 连接，只有 `name` 有 trigram GIN（0086，且是
`entity_type='SUPPLIER'` 的 partial），`enterprise_code` / `source_code` **只有 btree**，
`%q%` 用不上 ⇒ `OR` 整条退化为 pkey 顺序扫 + 逐行过滤。

- 同 SQL 带 `entityType='SUPPLIER'`：**5.6 ms**（3499 行）⇒ 差 **60 倍**。
- 前端 `searchEntities(q, limit)`（`api/procurementKnowledge.ts:82`）**不传 entityType**
  ⇒ 用户搜索走的就是慢路径。
- dev 0.35s 尚在 1s 内；生产 `entity_mapping` 若到 100 万行量级，按线性外推 ≈ 1s，
  **会撞目标**。

**建议（未实施，需动迁移）**：`enterprise_code` / `source_code` 补 trigram GIN
（03 号迁移之后的 `0162_entity_mapping_code_trgm`），让三路 ILIKE 走 BitmapOr；
或前端搜索固定带 entityType（但会砍掉跨类型检索能力，不推荐作为唯一修法）。
