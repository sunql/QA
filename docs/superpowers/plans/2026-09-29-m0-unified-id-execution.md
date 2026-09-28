# M0 Unified ID 后续执行计划

> **状态**：M0-P0.1 (id_mapping 表 + API) ✅ 已合入 feat/m0-unified-id（36f1ddc）
> **本次完成**：M0-P0.1 写路径改造全部 CRITICAL 修复（backfillRelations / syncMissingGraph / rebind_ontology_thbi.py）
> **下一步**：提交 PR feat/m0-unified-id → main，然后继续 M0-P0.3/P0.4

---

## 剩余任务总览

| 任务 | 分支 | 目标 | 前置条件 |
|------|------|------|----------|
| M0-P0.3 Neo4j 回填 | feat/m0-unified-id | 三类节点 external_id 全量写入 Neo4j | M0-P0.1 |
| M0-P0.4 Milvus 回填 | feat/m0-unified-id | 三 collection 加 milvus_id 字段 | M0-P0.1 |
| feat/planner-step-limit 合入 | feat/planner-step-limit → epic/v31-upgrade | M2a Planner ≤5 步硬限上线 | 无 |
| epic/v31-upgrade 合入 main | epic/v31-upgrade → main | 聚合 B1 + A6 + 后续 | B1 + A6 |

---

## M0-P0.3 · Neo4j external_id 回填

### 目标
对 Neo4j 中已有的 Class/Property/Metric 节点，写入 `external_id` 属性（值 = unified_id），使 Neo4j 侧与 PG id_mapping 对齐。

### 实现方式
- 扫描 Neo4j 全量节点（`MATCH (n) WHERE n:Class OR n:Property OR n:Metric`）
- 按 `n.id` 数值查 id_mapping（`COALESCE(unified_id, 'obj:' + label + ':' + toString(n.id))`）
- 对无 unified_id 的旧节点，生成占位 unified_id 并写入 PG id_mapping + Neo4j `external_id`

### 文件
- `scripts/backfill_neo4j_external_id.py`（新建）

### 验证
- `scripts/reconcile_neo4j_id_mapping.py`（新建）：对比 PG id_mapping 与 Neo4j external_id，diff 为空则通过

---

## M0-P0.4 · Milvus external_id 回填

### 目标
Milvus 三个 collection（`ontology_class_embeddings` / `ontology_property_embeddings` / `ontology_metric_embeddings`）中的文档补录 `milvus_id = unified_id` 字段。

### 实现方式
- 新增 collection 带 `external_id` field，迁移脚本双写（不删旧 collection）
- 或对现 collection 全量扫描，按 `id` 查 PG 回填（需 Milvus 支持 upsert）

### 文件
- `scripts/backfill_milvus_external_id.py`（新建）

---

## PR 提交顺序

```
1. feat/m0-unified-id  → main        （M0-P0.1 写路径改造 + 本次 CRITICAL 修复）
2. feat/planner-step-limit → epic/v31-upgrade（M2a Planner ≤5 步硬限）
3. epic/v31-upgrade → main           （B1 evidence + A6 planner + feat/m0-unified-id 已合入的部分）
```

---

## Alembic 版本号

| 版本 | 内容 | 状态 |
|------|------|------|
| 0097 | id_mapping 表 + API | ✅ 已合入 feat/m0-unified-id |
| 0098 | claim confidence_level 派生列（乙 B4） | 待乙占用 |
| 0099 | claim source_version（甲 A1 收尾） | 待占 |

---

## 风险

| 风险 | 缓解 |
|------|------|
| Neo4j 节点数量大（>10k），单次 MERGE 超时 | 分批处理，每批 500 条，计数对账 |
| Milvus external_id 迁移需 re-load collection | 使用 dual-collection 双写窗口，灰度切换 |
| id_mapping 与 Neo4j/Milvus 不一致 | 三方 diff 脚本每日对账 |
