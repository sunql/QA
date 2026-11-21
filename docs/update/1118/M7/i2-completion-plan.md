# I2 知识服务 → 100% 完成计划（2026-11-21 实测版）

> 基线 commit `0811ae43` ｜ dev 栈真实 HTTP 查询 ｜ 对应 §12-I2 与 §4.1–4.3

## 一、现状实测（四通道真实查询）

| 查询 | 通道 | 结果 |
|---|---|---|
| 「找出可以供应不锈钢板的供应商」 | GRAPH/SQL/RAG/METRIC | **0 证据**；GRAPH=EMPTY、SQL=EMPTY、METRIC=EMPTY、**RAG=DEGRADED(`v2_missing`)** |
| 「供应商主数据有哪些」 | GRAPH | **5 证据 OK** |
| 「供应商准时交付率」 | METRIC / SQL | METRIC **1 证据 OK** / SQL EMPTY |
| 「收货单宽表」 | GRAPH | EMPTY（别名是「DWD收货单宽表」，带前缀） |

⇒ 路由链路本身是通的，**卡在"自然问法召不回"**。

## 二、根因五条（按影响排序）

1. **匹配过严（主因）**：GRAPH/SQL 用 `matching.matchTerms`，语义是「**类词项 ⊂ 问句**」的精确包含。
   别名「供应商主数据」不会被「供应商」命中；别名带 `DWD` 前缀（「DWD收货单宽表」）挡掉自然说法。
   METRIC 只在**指标名精确出现**时命中。
2. **RAG 路 `v2_missing`**：registry v2 向量集合缺失 ⇒ 声明了作用域时**拒绝**（`semantic_registry_rag_gate.py:269`）。
3. **Evidence 契约不全**：缺 §4.2 的 `summary`；`sourceRef` **只有 DOCUMENT 路回填**，
   GRAPH 缺 `syncBatch`、SQL 缺 `datasourceId/queryId/sqlHash/executedAt`、
   METRIC 缺 `period/grain`、DOCUMENT 缺 `page/chunkId`。
4. **冲突检测缺失**：§4.3 明确要求「证据冲突时输出冲突提示与来源时间，**不让模型自选**」——
   当前 facade 只有 warnings 聚合，无 conflicts 结构。
5. **语料为空**：`document_catalog` 仅 3 条 OTHER、**无采购制度/合同** ⇒ DOCUMENT 通道恒空、Q04 空。

## 三、三批执行

### I2-A 匹配召回（代码，0.5–1 天）★ 决定 80% 的收益
- **复用** `services/recall_keyword_anchor.selectAnchoredMatches`
  （2–6 字 n-gram、问句片段 ⊂ 类名/别名、`isExact`+`fragmentLength` 打分）——
  与 chat 召回（#157/#158）**同一套语义**，不另造。
- 三路统一接入：GRAPH / SQL / METRIC（METRIC 保留「精确名优先」档）。
- **别名去前缀**：匹配前剥 `DWD_ / DIM_ / ODS_`（「DWD收货单宽表」→「收货单宽表」）。
- 验收（硬）：
  ①「找出可以供应不锈钢板的供应商」⇒ **≥2 通道出证据**（当前 0）；
  ②「收货单宽表」⇒ 命中 `DWD_GOODS_RECEIPT_DTL`；
  ③ 既有 5 个知识路单测全绿（放宽匹配不许打破精确用例）。

### I2-B 契约与冲突（代码，1 天）
- Evidence 补 `summary`；`sourceRef` 扩 `page / chunkId / syncBatch / sqlHash /
  executedAt / paramsSummary / period / grain`。
- 三路**回填 sourceRef**（现在只有 DOCUMENT 路有）：GRAPH=节点键+关系类型+同步批次；
  SQL=datasourceId+queryId+SQL hash+执行时间；METRIC=metricCode+period+grain。
- **冲突检测**（§4.3）：同实体/同指标跨通道数值不一致 ⇒ `conflicts[]`（两来源 + 各自时间），
  并入响应 warnings；**不合并成一个值**。
- 验收：契约逐字段用例；构造「GRAPH 与 SQL 给出不同值」的用例 ⇒ 必出 conflict。

### I2-C 索引与语料（运营 + 内容）
- 建 **registry v2 向量索引**（消除 `v2_missing`）⇒ RAG 路恢复 `OK`。
- 导入采购制度/合同 ≥10 份 ⇒ DOCUMENT 通道有命中、**Q04 解锁**。
- （可选）给 DIM_SUPPLIER 等核心类补短别名（「供应商」「物料」）——双保险。

## 四、完成判据（I2=100%）
1. 一次「供应商寻源」查询 ⇒ **≥2 通道证据**，每条带 `evidenceId + security + sourceRef`
2. RAG 路 `status=OK`（无 `v2_missing`）
3. 冲突用例：两路数值不一致 ⇒ 响应含 `conflicts[]`（含来源与时间）
4. 权限隔离 E2E：域不可见 ⇒ 空 + `MSG_DOMAIN_NOT_VISIBLE`；文档密级不匹配 ⇒ 不返回
5. Q04 制度类问题有证据（依赖 I2-C 语料）

## 五、依赖
| 批次 | 依赖 | 谁做 |
|---|---|---|
| I2-A / I2-B | 无（纯代码） | 工程 |
| I2-C 索引 | 无（本地可建） | 工程/运营 |
| I2-C 语料 | **采购制度/合同文档** | 业务提供 |
