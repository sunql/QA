# 变更：feat-semantic-relations

- **日期**：2026-08-31
- **作者**：Claude (Phase 6.2)
- **Phase**：6.2（业务语义关系入图）
- **状态**：done

## 1. 需求

把 Neo4j 从「本体图」（Class/Property/Metric）扩展为「业务关系图」：采购域业务实体（供应商/物料/订单/收货/检验/NCR/合同）作为节点入图，Sheet 16 业务流转语义作为边，为 6.3 多跳推理 API 提供「供应商->物料->订单->收货->问题」链路数据底座。

验收标准：
- 新增关系类型（白名单）：`SUPPLIES / CONTAINS / GENERATES / INSPECTED_BY / GENERATED / SIGNED`。
- 新增业务实体 label（白名单）：`Supplier / Material / PurchaseOrder / GoodsReceipt / IncomingInspection / NCR / Contract`。
- 数据来源三路：`entity_mapping`（跨系统主数据）+ `document_catalog`/`document_entity_relation`（合同关联）+ Sheet 16 演示流转。
- 业务图与本体图**物理隔离**：业务节点统一携带 `BusinessEntity` 主 label；本体同步/删除路径（`_ALLOWED_LABELS`）永不触碰业务节点；删除业务图不影响本体。
- 一次性脚本 `scripts/seed_graph_relations.py` 幂等回填（重复运行节点/边数量不变）。
- Neo4j ≥30 条业务关系（Phase 6 验收线）。
- CQL label / 关系类型不可参数化 -> 白名单防注入。
- 覆盖率 ≥80%；Neo4j 不可达时集成测试跳过（不阻断 PG，ai-roadmap §6 关键约束）。

## 2. 设计评审

**关键设计决策**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 图隔离方式 | 业务节点双 label：`BusinessEntity` + 具体类型 | plan 风险表原话「新增 BusinessEntity label 区分」；本体路径（upsertClassNode/deleteNode/listNodesByLabel）全部按 `_ALLOWED_LABELS` 过滤，天然不触碰业务子图；`deleteBusinessGraph` 只 MATCH BusinessEntity，删除范围封闭 |
| 节点唯一键 | `key` = enterprise_key 字符串化（Contract 用 document_id） | entity_mapping 的 enterprise_key 是跨系统实体代理键（Phase 3 SSOT）；MERGE on key 幂等 |
| 边写入语义 | 两端 MATCH（非 MERGE）+ 边 MERGE | 防止边写入悄悄创建孤立节点；MERGE (a)-[r]->(b) 保证幂等 |
| label/relType 防注入 | 双白名单 `BUSINESS_ENTITY_LABELS` / `BUSINESS_RELATION_TYPES`，拼接前 `_assertBusiness*` 校验 | CQL 标签/关系类型不可参数化，与 `_ALLOWED_LABELS` 同模式（deleteNode 已有先例） |
| 边数据来源 | 静态确定性派生（Sheet 16 演示流）+ PG 真实关联（SIGNED） | instance 级业务流转数据仓库中不存在（DWD 明细表无实例回填），与 seed_entity_mapping 的 Sheet 04/05 静态种子同模式；键位与 entity seed 完全对齐 |
| SUPPLIES 派生规则 | 供应商 i 供应物料 {(3i-2,3i-1,3i) mod 10}，共 30 条不重复 | 确定性（重放稳定）；每供应商恰好 3 物料，30 条单独满足 Phase 6 验收线 |
| 服务层归属 | `GraphRelationService`（app/services）+ 脚本薄壳 | 派生逻辑可单测；与 ontology_service 平级；脚本仅做种子编排 |
| 集成测试策略 | 真实 PG + **真实 Neo4j**（模块级 `isNeo4jAvailable` 探测，不可达整文件 skip） | 6.2 的核心交付物就是图数据，mock 图库验证不了 MERGE 幂等/隔离；skip 保证「Neo4j 失败不阻断 PG」约束 |
| REST API | 本期不暴露（Phase 6.3 交付 traversal API） | plan 6.2 关键文件清单不含 api/v1 扩展；避免超前设计 |
| 前端 | 本期无页面（Neo4jGraphPage 已有通用图渲染，6.3 接入业务图） | plan 6.2 无前端条目 |

## 3. 数据模型变更

**无新表、无迁移**。读取既有 3 张表：

- `entity_mapping`（Phase 3.1）：25 实体（10 供应商 ×多源去重 + 10 物料 + 3 PO + 1 GR + 1 IQC）-> 业务实体节点；
- `document_catalog` + `document_entity_relation`（Phase 5.1）：CONTRACT 类型关联 -> Contract 节点 + SIGNED 边。

**Neo4j 业务子图 schema**（无 PG 等价物，图库原生）：

```
(:BusinessEntity:Supplier {key, code, name, entityType, source})
(:BusinessEntity:Material {...})
(:BusinessEntity:PurchaseOrder {...})
(:BusinessEntity:GoodsReceipt {...})
(:BusinessEntity:IncomingInspection {...})
(:BusinessEntity:NCR {...})
(:BusinessEntity:Contract {key: document_id, ...})

(:Supplier)-[:SUPPLIES {source}]->(:Material)          30 条
(:PurchaseOrder)-[:CONTAINS {source}]->(:Material)      9 条
(:PurchaseOrder)-[:GENERATES {source}]->(:GoodsReceipt) 3 条
(:GoodsReceipt)-[:INSPECTED_BY {source}]->(:IncomingInspection) 2 条
(:IncomingInspection)-[:GENERATED {source}]->(:NCR)     1 条
(:Supplier)-[:SIGNED {source}]->(:Contract)             PG 关联驱动（当前 2 条）
```

节点 `source` 属性标记来源（`entity_mapping` / `sheet16_demo` / `document_catalog`），边 `source` 标记 `sheet16_demo` / `document_entity_relation`，便于溯源。

`seed_entity_mapping` / `document_entity_relation` 种子补充：脚本主流程先幂等重放 entity 种子 + 新增 2 条 doc 关联（DOC-SMOKE-001/002 -> 供应商 100001/100002，`INSERT ON CONFLICT DO NOTHING`）。

## 4. 关键代码

**后端**：

- `backend/app/infrastructure/neo4j_client.py`（+~200 行）：
  - 常量：`BUSINESS_ENTITY_LABELS`（7 label）、`BUSINESS_RELATION_TYPES`（6 关系）；
  - 校验：`_assertBusinessLabel` / `_assertBusinessRelation`（ValueError）；
  - 节点：`upsertBusinessEntityNode(label, key, code, name, source)` —— MERGE on key + 双 label；
  - 边：`linkBusinessRelation(relType, fromLabel, fromKey, toLabel, toKey, properties)` —— MATCH 两端 + MERGE 边 + SET props；
  - 生命周期：`deleteBusinessGraph()`（仅 BusinessEntity，DETACH DELETE）、`countBusinessNodes()`、`countBusinessRelations()`；
  - 查询：`getBusinessGraphSnapshot()`（nodes + edges 全量快照，6.3 遍历 / 前端渲染契约）；
  - 探测：`isNeo4jAvailable()`（连接探测，供测试 skip）。
- `backend/app/services/graph_relation_service.py`（NEW ~290 行）：
  - `ENTITY_TYPE_LABELS`：EntityType -> Neo4j label 映射（6 项）；
  - `GraphSeedResult`（frozen dataclass）：nodeCount/edgeCount/nodesBySource/edgesByType；
  - `seedGraphRelations(session)`：三路数据源编排（entity_mapping 去重节点 + sheet16 补充节点 + 文档 SIGNED 边 + Sheet 16 流转边）；
  - `_dedupeByType`：同实体多源系统映射去重（enterprise_key 维度）；
  - `_signedContractPairs`：document_entity_relation JOIN document_catalog（SUPPLIER + CONTRACT）；
  - `_sheet16Edges`：45 条流转边确定性派生（SUPPLIES 30 / CONTAINS 9 / GENERATES 3 / INSPECTED_BY 2 / GENERATED 1）；
  - `validateSchema()`：service 常量与 neo4j_client 白名单双向防漂移；
  - `getGraphSnapshot()` / `resetGraph()`：委托 client。
- `backend/scripts/seed_graph_relations.py`（NEW ~130 行）：CLI 种子（schema 校验 -> entity_mapping 幂等重放 -> doc 关联种子 -> 图回填 -> 30 条边验收线检查，不足 sys.exit(1)）。

**测试**：

- `backend/app/tests/unit/test_graph_relation_service.py`（NEW，18 用例）：mock Neo4j（记录 CQL 供断言）+ fake PG rows；白名单 6 项（含 CQL 注入尝试）/ Sheet 16 派生 4 项 / seed 编排 6 项（去重、CQL 结构、MATCH-not-MERGE、SIGNED、空数据兜底）/ 生命周期 2 项。
- `backend/app/tests/integration/test_graph_relations_integration.py`（NEW，7 用例）：**真实 PG 5433 + 真实 Neo4j 7687**；模块级 skipif（Neo4j 不可达）；每用例前后 `deleteBusinessGraph` 清场；覆盖端到端计数 / 幂等（双次 seed 数量不变）/ 图隔离（本体 Class 数全程不变）/ 多跳链路（100001 三跳供应链 + PO->GR->IQC 流转链）/ SIGNED 需 PG 关联（含 join 语义验证）/ 多源去重（45 映射 -> 10 供应商节点）/ 快照 schema 契约。

## 5. 测试

**执行结果**（真实环境，2026-08-31）：

- 单元：18 passed（0.05s）
- 集成：7 passed（2.99s，真实 PG + 真实 Neo4j）
- 回归：Phase 6.1（agent registry）23 用例 + ontology API 51 + data lineage 单测 = 全部通过，无回归
- 覆盖率：`graph_relation_service.py` 95%（81 语句，4 未覆盖 = validateSchema 防漂移分支）

**手动验证**（真实库）：

- 种子脚本首跑：节点 31（entity_mapping 25 + sheet16_demo 4 + document_catalog 2），边 47（SUPPLIES 30 + CONTAINS 9 + GENERATES 3 + INSPECTED_BY 2 + SIGNED 2 + GENERATED 1）——超过 Phase 6 的 30 条验收线。
- 幂等重跑：数量不变（31/47），entity_mapping/doc relation 新增 0 条。
- 图隔离：业务图写入+清空前后本体 Class 节点数恒为 34。
- 多跳链路：`Supplier {key:'100001'}-[:SIGNED]->()` 1 条；`Supplier-[:SUPPLIES]->Material<-[:CONTAINS]-PurchaseOrder` 3 条路径。

## 6. 安全审查

- **CQL 注入**：label / 关系类型不可参数化 -> 双白名单 + 拼接前校验（`_assertBusinessLabel` / `_assertBusinessRelation`）。注入尝试（label 内嵌 CQL 片段）单测覆盖 -> ValueError。所有参数值（key/code/name/props）走 `$param` 绑定。
- **图隔离**：业务 label 与本体 `_ALLOWED_LABELS` 无交集（单测断言）；`deleteBusinessGraph` 只 MATCH `BusinessEntity`。
- **无新 API 端点**：无 attack surface 变化；无用户输入进入 CQL（seed 数据全部来自 PG 白名单键位 + 静态常量）。
- **无凭据泄漏**：沿用 `_sanitizeUri` 日志脱敏。
- 本期无认证/授权变更（图写入仅 seed 脚本触发，不经 HTTP）。

## 7. 部署与迁移

```bash
cd backend

# 无 Alembic 迁移（无新表）；仅需 Neo4j 容器运行（docker compose qa-neo4j，bolt 7687）
.venv/bin/python -m scripts.seed_graph_relations
# 期望输出：节点 31，边 47；不足 30 条边时脚本 exit(1)

TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
  .venv/bin/pytest app/tests/unit/test_graph_relation_service.py \
    app/tests/integration/test_graph_relations_integration.py -v
# 期望：25 passed（Neo4j 可达时）；Neo4j 不可达时集成 7 用例 skip

# 验证图状态
docker exec qa-neo4j cypher-shell -u neo4j -p <密码> \
  "MATCH (b:BusinessEntity) RETURN b.entityType AS type, count(*) AS cnt;"
```

无新 Python 包依赖、无环境变量新增。

**注意**：集成测试与真实库共用同一 Neo4j 实例（fixture 前后 `deleteBusinessGraph` 清场），跑完测试后需重跑 seed 脚本恢复业务图。

## 8. 关键文件清单

**新增**：
- `backend/app/services/graph_relation_service.py`
- `backend/scripts/seed_graph_relations.py`
- `backend/app/tests/unit/test_graph_relation_service.py`
- `backend/app/tests/integration/test_graph_relations_integration.py`

**修改**：
- `backend/app/infrastructure/neo4j_client.py`（业务子图常量 + 8 个函数，~+200 行）

## 9. 验证

Phase 6.2 验收标准逐项核对：

- ✅ 6 种业务关系类型 + 7 种业务实体 label 白名单落地（SUPPLIES/CONTAINS/GENERATES/INSPECTED_BY/GENERATED/SIGNED）
- ✅ 三路数据源（entity_mapping + document_catalog/entity_relation + Sheet 16）
- ✅ 图隔离验证（本体节点全程不受影响，单测 + 集成测双重覆盖）
- ✅ 种子脚本幂等（双次运行 31/47 不变）
- ✅ Neo4j 47 条业务关系 ≥ 30 条（Phase 6 验收线）
- ✅ CQL 防注入（双白名单 + 注入尝试单测）
- ✅ 覆盖率 95% ≥ 80%
- ✅ Neo4j 不可达时跳过（不阻断 PG）
- ✅ 多跳链路可用（100001 三跳供应链 + PO->GR->IQC->NCR 流转链），6.3 traversal API 数据底座就绪

## 10. 已知缺口 / 后续 Phase

- **业务实例边真实化**：当前 SUPPLIES/CONTAINS 等边是 Sheet 16 确定性演示数据；待 DWD 明细表（采购订单行/收货行）有真实实例数据后，可从 `ontology_join` 指向的业务表回填真实边（本期 plan 允许「Excel Sheet 16 数据回填」）。
- **ontology_join 类级关联入图**：56 条类级 JOIN（foreign_key 46 + business 10）未入图；6.3 遍历 API 如需类级推理可补充 `(:Class)-[:JOIN]->(:Class)` 边（本期 plan 范围外）。
- **业务图 REST API**：`getBusinessGraphSnapshot` 已就绪但未暴露 HTTP；Phase 6.3 `feat-graph-traversal-api` 交付 `GET /graph/traverse` 时一并暴露。
- **Chat 集成**：「供应商 100001 涉及哪些物料」类推理问法路由到图遍历，留 6.3。
- **业务图与本体 Class 关联**（`(:BusinessEntity)-[:INSTANCE_OF]->(:Class)`）：未做，避免两图耦合（风险表要求隔离）；如 6.3 需要可再评估。
