# feat-business-data-reset — 业务数据全量重置

**Date**: 2026-09-19
**Status**: 已设计，待执行
**Owner**: sunql
**Risk**: 高（不可逆 + 影响三库一致性）

---

## Context

prod `qa_metadata` 上积累了多轮手动导入 + seed 写入，业务表的真实结构已偏离「种子状态」（尤其 `ontology_class` 96→32 是 recall pruning 后状态、wiki_page 36 含重复导入、`ontology_class_pre_pruning_20260918` 备份表遗留、entity_mapping 同步曾因 hardcode 数据源名 bug 漏跑）。

用户要求：**把 PG 现有的本体类/属性/关系/血缘/质量规则等业务数据 + Neo4j/Milvus 同步数据，全部清掉，重新初始化业务库**。

**范围决策**（已与用户对齐）：

| 决策点 | 用户选择 |
|---|---|
| 清理范围 | 本体 + 知识库 + 血缘 + 质量 + 文档 + 评估（全量业务数据） |
| 备份策略 | 全库 `pg_dump` 到 `backups/pg/`（沿用现有备份链） |
| 清库顺序 | PG 先清 → Neo4j 清本体子图 → Milvus 清本体/知识库 collection |
| seed 范围 | 业务数据全重跑 seed；本体走导入向导人工重导 |
| 验证门 | 三库对账 + smoke test 都能走通 |
| 本体源 | 走导入向导人工重导（脚本不灌本体） |

**明确不删**（保留作系统配置）：

- `users / roles / user_roles / user_organizations / organizations / permission_grant`
- `data_source / llm_config / embedding_provider`
- `menu_config / agent_definition / agent_tool_config / agent_access_policy / agent_schedule / agent_run_log`
- `system_config / term_dictionary / schema_cache`
- `audit_log / audit_outbox / learning_feedback`
- `session_message / session_query_state / session_token_usage / wiki_token_usage / wiki_rule_executable`
- **遗留快照**：`ontology_class_pre_pruning_20260918`（recall pruning 时备份的 27 语义类——是过程快照不是业务数据）

---

## 设计

### Step 1 · 全库备份

```bash
TS=$(date +%Y-%m-%d_%H%M)
docker exec qa-postgres pg_dump -U qa_user -d qa_metadata \
  --inserts \
  > backups/pg/qa_metadata_${TS}_pre_reset.dump
```

备份路径沿用 `backups/pg/qa_metadata_<YYYY-MM-DD_HHMM>*.dump` 既有命名链。最近一份是 `qa_metadata_2026-09-19_1130.dump`。

### Step 2 · PG 业务表清空（24 张）

单条 `TRUNCATE ... RESTART IDENTITY CASCADE`——CASCADE 处理依赖，Redis 处理大表：

```sql
TRUNCATE
  -- 评估 + 分享
  evaluation_report_share,
  evaluation_report_schedule,
  evaluation_report,
  -- 文档 + 证据
  document_entity_relation,
  document_catalog,
  evidence,
  -- 知识发现
  structure_suggestion,
  knowledge_conflict,
  knowledge_community_member,
  knowledge_community,
  knowledge_relation,
  knowledge_claim,
  -- wiki
  wiki_graph_insight,
  wiki_compile_item,
  wiki_compile_task,
  wiki_import_task,
  wiki_page,
  -- in-app
  in_app_message,
  -- KPI + 特征
  kpi_catalog_history,
  kpi_catalog,
  feature_definition_history,
  feature_value,
  feature_rule,
  feature_definition,
  -- DQ
  data_quality_violation_sample,
  data_quality_score,
  data_quality_rule,
  -- 血缘
  data_lineage,
  -- 实体映射
  entity_mapping
RESTART IDENTITY CASCADE;
```

**本体相关表（7 张）单独再清一次**——不放在上面 TRUNCATE 里是为了让审计/排查能看清「业务表 17 张 → 本体 7 张」：

```sql
TRUNCATE
  ontology_metric,
  ontology_relation,
  class_domain_mapping,
  business_object,
  ontology_join,
  ontology_property,
  ontology_class
RESTART IDENTITY CASCADE;
```

**不删 `ontology_class_pre_pruning_20260918`**——它是中间快照，不是业务数据。

执行方式：写到 `backend/scripts/reset_business_data.sql`，**只用 psql 一次性执行**（不进 ORM 模型层——一次性脚本不进版本树）。脚本前后 `BEGIN; COMMIT;` 包裹便于失败回滚。

### Step 3 · Neo4j 清本体子图

只清 ontology 相关节点 + 关系（避免误删 ACL / 用户 / 审计等图数据）：

```cypher
// 先确认标签再执行
CALL db.labels() YIELD label
WHERE label STARTS WITH 'Ontology'
RETURN label;

MATCH (n)
WHERE any(l IN labels(n) WHERE l STARTS WITH 'Ontology')
DETACH DELETE n;
```

具体标签名（`OntologyClass` / `OntologyProperty` / `OntologyJoin` / `OntologyRelation` / `OntologyMetric`）执行前先 `CALL db.labels()` 查实际标签再清——避免漏清。

执行方式：写到 `backend/scripts/reset_neo4j_ontology.cypher`，进容器内 `cypher-shell -u neo4j -p <pwd>` 执行。

### Step 4 · Milvus 清四套 collection

```python
# backend/scripts/reset_milvus_collections.py
from app.infrastructure.milvus_client import (
    ontology_embeddings, entity_mapping_embeddings,
    wiki_page_embeddings, document_embeddings,
)
for col in (ontology_embeddings, entity_mapping_embeddings,
            wiki_page_embeddings, document_embeddings):
    col.drop()
```

执行前先 `print(col.name, col.num_entities)` 确认 collection 名；只清上面四个，**不动 `dq_evaluator` 等其他业务 collection**（如存在）。执行后 `utility.list_collections()` 应为空（除非有其他没在脚本里的 collection）。

### Step 5 · 重跑 seed 脚本

按依赖顺序：

```bash
# 1. 数据源（不重置——已存在 4 条）
#    [跳过 seed_oracle_datasource.py]

# 2. 业务别名 + 业务编码（轻量）
docker exec qa-backend python -m scripts.seed_business_aliases.py
docker exec qa-backend python -m scripts.seed_business_objects.py
# 重新走 API 入 Neo4j/Milvus [本体为空，business_object/class_domain_mapping 也为空，
# 这两个脚本走的是 IDENTITY 维度，本身不依赖本体 32 类，可正常跑]

# 3. 实体映射
docker exec qa-backend python -m scripts.sync_entity_mapping_from_thbi.py
docker exec qa-backend python -m scripts.seed_entity_mapping.py  # 兜底

# 4. KPI / Feature / Quality
docker exec qa-backend python -m scripts.seed_kpi_catalog.py
docker exec qa-backend python -m scripts.seed_features.py
docker exec qa-backend python -m scripts.seed_feature_rules.py
docker exec qa-backend python -m scripts.seed_data_quality_realdata.py

# 5. 血缘（按需）
docker exec qa-backend python -m scripts.lineage_auto_extract.py

# 6. [手动] 本体 32 类：走导入向导（UI 或 API）从 enterprise-data-knowledge-architecture-editable.pptx
#    完成后通过 ontology_service API 自动入 Neo4j/Milvus
```

**本体走导入向导的预期**：用户从仓库根 `enterprise-data-knowledge-architecture-editable.pptx` 走 UI 导入。导入完成后本体 32 类 + 3148 属性 + 4 join 会自动通过 `createClass/createProperty/createJoin` API 入三库（这是 [[qa-system-neo4j-ontology-empty]] 教训的关键：只有 API 路径会同步三库）。

### Step 6 · 验证（对账 + smoke）

```bash
# 6.1 PG 行数对账
docker exec qa-postgres psql -U qa_user -d qa_metadata -c "
SELECT 'ontology_class' tbl, count(*) cnt FROM ontology_class UNION ALL
SELECT 'ontology_property', count(*) FROM ontology_property UNION ALL
SELECT 'ontology_join', count(*) FROM ontology_join UNION ALL
SELECT 'data_lineage', count(*) FROM data_lineage UNION ALL
SELECT 'wiki_page', count(*) FROM wiki_page UNION ALL
SELECT 'knowledge_claim', count(*) FROM knowledge_claim UNION ALL
SELECT 'document_catalog', count(*) FROM document_catalog UNION ALL
SELECT 'evaluation_report', count(*) FROM evaluation_report UNION ALL
SELECT 'entity_mapping', count(*) FROM entity_mapping;
"
# 期望：本体 3 表 = 0（待手动导入），其他表 = seed 预期行数

# 6.2 Neo4j 标签对账
curl -u neo4j:<pwd> http://localhost:7474/db/data/labels
# 期望：清后无 OntologyClass / OntologyProperty；导入本体后 = PG ontology_class.count()

# 6.3 Milvus collection 对账
docker exec qa-backend python -c "
from app.infrastructure.milvus_client import ...
print('ontology_embeddings:', ontology.num_entities)
print('wiki_page_embeddings:', wiki.num_entities)
"
# 期望：本体导入后 == 32；wiki_page_embeddings == 0（清后无 wiki）

# 6.4 业务冒烟（本体已导入）
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"sessionId":"reset","datasourceId":1,"question":"公司有多少供应商","model_id":3}' | jq '.classRecall'
# 期望：含 DIM_SUPPLIER 候选；classCount > 0
```

**完成判据**：6.1 + 6.2 + 6.3 三库对账全过 + 6.4 smoke 返回含 DIM_SUPPLIER 的合理候选列表。

---

## 改动清单

| 文件 | 类型 | 用途 |
|---|---|---|
| `backend/scripts/reset_business_data.sql` | 新建 | Step 2 — PG 24 表 TRUNCATE |
| `backend/scripts/reset_neo4j_ontology.cypher` | 新建 | Step 3 — Neo4j 本体子图清理 |
| `backend/scripts/reset_milvus_collections.py` | 新建 | Step 4 — Milvus 4 collection drop |
| `backups/pg/qa_metadata_<TS>_pre_reset.dump` | 产出 | Step 1 — 全库备份 |
| `Harness/changes/feat-business-data-reset/summary.md` | 本文件 | 设计 + 执行清单 |
| `memory/qa-system-business-data-reset.md` | 新建 | 记忆条目（教训 + 校验清单） |
| `memory/MEMORY.md` | 修改 | 加索引 |

**不写测试**——一次性手动脚本，不进版本树，无回归价值。

---

## 执行流程（用户提交时严格按此顺序）

```bash
# 0. 通知用户准备执行（这个任务由「我自己提交」约束触发——用户执行，我写脚本）
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system

# 1. 全库备份
TS=$(date +%Y-%m-%d_%H%M)
docker exec qa-postgres pg_dump -U qa_user -d qa_metadata \
  --inserts \
  > backups/pg/qa_metadata_${TS}_pre_reset.dump

# 2. PG 清表（容器内 psql，user 不需要密码是 docker exec 模式）
docker exec -i qa-postgres psql -U qa_user -d qa_metadata \
  < backend/scripts/reset_business_data.sql

# 3. Neo4j 清本体子图（先 labels() 确认标签，再清）
docker exec qa-neo4j cypher-shell -u neo4j -p $(docker exec qa-neo4j env | grep NEO4J_AUTH | cut -d= -f2 | cut -d/ -f2) \
  "MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'Ontology') DETACH DELETE n;"

# 4. Milvus 清四套 collection
docker exec qa-backend python -m scripts.reset_milvus_collections

# 5. 重跑 seed
docker exec qa-backend python -m scripts.seed_business_aliases.py
docker exec qa-backend python -m scripts.seed_business_objects.py
docker exec qa-backend python -m scripts.sync_entity_mapping_from_thbi.py
docker exec qa-backend python -m scripts.seed_kpi_catalog.py
docker exec qa-backend python -m scripts.seed_features.py
docker exec qa-backend python -m scripts.seed_feature_rules.py
docker exec qa-backend python -m scripts.seed_data_quality_realdata.py

# 6. 验证（对账 + smoke）

# 7. [手动] 走导入向导导入 enterprise-data-knowledge-architecture-editable.pptx
```

---

## 风险与回滚

| 风险 | 等级 | 缓解 |
|---|---|---|
| TRUNCATE 误删系统表 | 极高 | TRUNCATE 列表明确 24 张 + 7 张本体；脚本写死在 SQL 里，不接受外部参数 |
| Neo4j 清本体子图误删 ACL/user 图 | 高 | `WHERE label STARTS WITH 'Ontology'` 严格过滤 |
| Milvus collection 名变化 | 中 | 脚本先 print collection 列表确认；执行前先 `--dry-run` |
| seed 脚本对本体有依赖 | 中 | seed 顺序：业务别名/编码 → entity_mapping → kpi/feature/quality；本体为空时 kpi/feature/quality 可能抛错——逐个跑、报错即停 |
| 本体未导入期间服务异常 | 低（计划内） | 计划内：本体由用户手动导入后才验收 smoke |
| 备份 dump 体积过大 | 低 | 现有链最大 dump ~7MB（PG 表小） |

**回滚路径**：

```bash
# 全库恢复（破坏性——会清掉重置后所有新 seed 数据）
docker exec -i qa-postgres psql -U qa_user -d qa_metadata \
  < backups/pg/qa_metadata_<TS>_pre_reset.dump
# 注：dump 用 --inserts 是纯 INSERT，不能直接 psql 灌；需 psql 解析 SQL。
# 如需精确恢复，drop schema public + alembic upgrade head + 灌 dump + 跑所有 seed
# 与「[[qa-system-pg-wipe-incident]]」恢复路径一致。
```

---

## 未做（明示）

- **本体 32 类不进 seed**——由用户在 UI 导入向导里走一遍，触发完整 API 路径同步 Neo4j/Milvus
- **drop schema public + alembic upgrade head**——不重做迁移，保留表结构（业务数据是值非 schema）
- **`ontology_class_pre_pruning_20260918` 备份表**——保留，作为历史快照
- **审计/agent_run_log/session**——保留作历史追溯
- **单元/集成测试**——一次性脚本无回归价值；后续如发现重置遗漏，再补 seed 脚本

---

## 验收（完成判据）

- [ ] `backups/pg/qa_metadata_<TS>_pre_reset.dump` 存在且可读
- [ ] PG 24 业务表 + 7 本体表 = 0 行（清后）
- [ ] Neo4j `Ontology*` 标签节点数 = 0（清后）
- [ ] Milvus 四套 collection 已 drop 或 num_entities = 0
- [ ] seed 全部跑完，业务表恢复 seed 预期行数
- [ ] 用户手动导入 32 类本体后：
  - PG ontology_class = 32, ontology_property = 3148, ontology_join = 4
  - Neo4j OntologyClass = 32
  - Milvus ontology_embeddings = 32
  - smoke `/chat` 返回含 DIM_SUPPLIER 的合理候选