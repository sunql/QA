# 变更：feat-business-graph-backfill（THBI 采购订单入业务图）

- **日期**：2026-10-25
- **作者**：Claude / 启琳
- **状态**：dev 已落地并真机验证（188,300 节点 / 519,265 边，10105 端到端遍历通过）；生产待用户执行（§5 runbook）
- **分支**：`feat/business-graph-backfill`（自 0f6c1cec）

## 0. 背景与根因

用户报障：业务图 tab 输入供应商编码 10105 点遍历 → 404「业务图中不存在该实体：Supplier 10105」。

| 探针 | 结果 |
|---|---|
| dev/prod Neo4j `BusinessEntity` 子图 | **0 节点** —— 404 根因，与查询代码无关 |
| `entity_mapping`（PG） | 10105 存在（`enterprise_key=191263012310605, enterprise_code='10105'`） |
| 写入链路断层 | `sync_entity_mapping_from_thbi` 只写 PG 不碰 Neo4j；业务图唯一回填入口 `seed_graph_relations.py` 是 Sheet 16 demo 键位（SUP000001/BPS0001 风格），与真实编码零交集 |

附：`loader.js.map` 404 是 DevTools 请求 sourcemap，生产 bundle 无 map，无害。

## 1. 数据源探针结论（只读，THBI Oracle X3V71ORA）

| 事实 | 数字 |
|---|---|
| `DWD_PURCHASE_ORDER`（单头） | 200,562 单，2012-12 ~ 2026-08 |
| `DWD_PURCHASE_ORDER_LINE`（行） | 927,648 行 |
| `DWD_GOODS_RECEIPT(_LINE)`（收货） | 97 万 / 355 万行（**本期不入图**） |
| 编码与 entity_mapping 交集 | 供应商/物料五维 **100%**（PO 头 1191/1191、PO 行物料 306,939/306,939、收货侧同） |
| distinct (supplier, material) 供应对 | 343,678 |
| PG 本地表 `PORDER`/`PORDERQ` | demo 种子（19 行，BPS0001 风格），与 THBI 编码零交集，勿混淆 |

探针脚本（只读，保留供生产预检复用）：
`scripts/probe_thbi_purchase_tables.py`（表清单+样本）、`scripts/probe_thbi_po_code_match.py`（交集率）。
注意 Oracle 拉全量 distinct 需 `QUERY_TIMEOUT_SECONDS=600`（默认 30s 会超时）。

## 2. 图形状与白名单

```
(:Supplier {key: 供应商编码})-[:PLACES]->(:PurchaseOrder {key: po_no})   ← PLACES 新增
(:Supplier)-[:SUPPLIES]->(:ItemMaster)      # distinct 供应对（label 是 Phase 4.4 后的 ItemMaster）
(:PurchaseOrder)-[:CONTAINS]->(:ItemMaster) # 行级
```

- 节点 key 口径不变：Supplier/ItemMaster 用 `enterprise_code`（与遍历入口、demo 图一致）；PO 用 `po_no`
- `BUSINESS_RELATION_TYPES` + `graph_relation_service.validateSchema` 同步加 `PLACES`（防漂移）

## 3. 实现（三块）

1. **`neo4j_client` 批量写**：`upsertBusinessEntityNodes` / `linkBusinessRelations`
   （UNWIND 分批 10k/事务，MERGE 幂等，COALESCE 保留已有 enrichment）；
   `ensureBusinessKeyIndex()`（`BusinessEntity(key)` 索引——**无索引时批量 MERGE
   退化 O(n²)，dev 实测中段明显变慢，建索引后恢复**）；遍历 CQL 加
   `LIMIT $pathLimit`（`_MAX_TRAVERSAL_PATHS=1000`，真实规模下 variable-length
   path 可能展开数十万行，必须封顶）。
2. **`scripts/sync_business_graph_from_thbi.py`**：`buildGraphPlan`（纯函数聚合：
   去重 / 供应商名 enrichment 首个非空 / 空编码与孤儿行跳过计数）+
   `applyGraphPlan`（节点先于边，`asyncio.to_thread` 包装）+
   `main`（`--dry-run` / `--since YYYY`，跑前 `validateSchema` 守卫）。
   **不可**复用 `seedGraphRelations` 的逐节点单 session MERGE（51 万节点是几十小时级）。
3. **杂项**：`buildChatAnswer` typeLabels 补 `ItemMaster/Receipt`（旧 Material 映射保留兼容）；
   前端业务图输入框 placeholder 改真实编码口径「如 10105」。

## 4. 测试与 dev 验证

| 项 | 结果 |
|---|---|
| 单测（buildGraphPlan 纯函数 + 批量写守卫 + 遍历封顶） | 70 passed |
| 集成（真实 Neo4j 7688：入图/幂等/遍历语义/白名单） | 4 passed；连同 graph relations/traversal API 22 passed |
| ruff | 0 error |
| dev dry-run `--since 2024` | headers=86,919 lines=318,540；跳过 0 孤儿 0 |
| dev 全量入图 | **188,300 节点 / 519,265 边**（PLACES 86,919 + SUPPLIES 113,806 + CONTAINS 318,540）|
| 10105 端到端 3 跳 | 1 跳：物料 1012000991 + PO T12605POH0092/T12606POH0001；2-3 跳 CONTAINS 链 ✅ |
| 测试环境 | `TEST_DATABASE_URL`(qa-pg-a1:5434) + `TEST_NEO4J_URI=bolt://localhost:7688`，业务图用例前后清子图，本体图零损伤 |

## 5. 生产 runbook（用户手工执行）

前提：生产 qa-backend 容器 ≥ 93abb0d7 + prod-hotfix-kit-1024 已上（entity_mapping 35.4 万已同步）。

```bash
# 0) 预检（可选但推荐）：确认 THBI 采购表编码仍 100% 交集
#    （需先拷入 probe 脚本；跳过亦可——探针结论见 §1）

# 1) 热修 4 个文件入容器（同 prod-hotfix-kit-1024 模式）
docker cp backend/app/infrastructure/neo4j_client.py        qa-backend:/app/app/infrastructure/neo4j_client.py
docker cp backend/app/services/graph_relation_service.py    qa-backend:/app/app/services/graph_relation_service.py
docker cp backend/app/services/graph_traversal_service.py   qa-backend:/app/app/services/graph_traversal_service.py
docker cp backend/scripts/sync_business_graph_from_thbi.py  qa-backend:/app/scripts/
docker restart qa-backend

# 2) dry-run 看计划（生产全量，不带 --since）
docker exec -e QUERY_TIMEOUT_SECONDS=600 qa-backend \
  python -m scripts.sync_business_graph_from_thbi --dry-run
# 期望：节点 ~51 万 / 边 ~147 万 量级，跳过接近 0

# 3) 正式回填（预计 30~60 分钟；QUERY_TIMEOUT 必须给 600，否则 92 万行拉取超时）
docker exec -e QUERY_TIMEOUT_SECONDS=600 qa-backend \
  python -m scripts.sync_business_graph_from_thbi

# 4) 验证：cypher 数数 + 页面遍历 10105
docker exec qa-neo4j cypher-shell -u neo4j -p '<密码>' \
  "MATCH (b:BusinessEntity) RETURN labels(b)[1], count(*)"
# 浏览器：业务图 tab 输入 10105 → 遍历 → 应见物料/采购订单链
```

回滚：遍历失败不影响任何存量功能（只读 API）；要清空入图数据执行
`MATCH (b:BusinessEntity) DETACH DELETE b`（本体图不受影响）。热修文件回滚 =
重建容器（或用 prod-hotfix-kit 同款备份法）。

## 6. 已知边界 / 后续

- **收货链路未入图**（97 万 GoodsReceipt 节点会让图翻倍，边际价值低）；
  将来接入时走 `DWD_GOODS_RECEIPT_LINE.po_no` 关联，边 GENERATES（白名单已有）。
- 遍历结果超 1000 行静默截断（LIMIT）；前端 hops 渲染量大时的体验优化不在本期。
- 合同（Contract）无数据源计划，SIGNED 边维持 demo 语义。
- 生产热修文件积累到 8 个 .py（前 4 + 本批 4）——**下次正式镜像部署统一收编**。
- demo 种子（seed_graph_relations）与真实数据共存：demo 节点 source=sheet16_demo，
  真实节点 source=thbi_dwd，可按 source 属性区分。
