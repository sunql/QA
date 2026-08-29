# 变更：Neo4j 本体图同步（#65）

- **日期**：2026-08-12
- **作者**：AI 助手
- **Phase**：Phase 5 之后差距补齐（设计稿"本体图"能力的实现缺口）
- **状态**：done

## 1. 需求

原设计中本体管理（Phase 2）应保持 PostgreSQL（关系型元数据）+ Neo4j（本体图）双写一致，但实现存在两个缺口：

1. **seed_ontology.py 只写 PG**：19 类 + 491 属性 + 外键关系从未同步到 Neo4j 图库。
2. **update 不写 Neo4j**：`updateClass`/`updateProperty`/`updateMetric` 只更新 PG，未同步图节点属性；而 create/delete 已具备 Neo4j 同步。

验收标准：
- 跑 `seed_ontology.py` 后，Neo4j 中出现 19 个 `:Class` 节点、491 个 `:Property` 节点、`HAS_PROPERTY`/`REFERENCES` 关系。
- 通过 API 更新类/属性/指标后，Neo4j 节点属性同步更新；属性 `ref_class_id`、指标 `target_class_id` 变化时，图上关系随之重建（无残留旧边）。

## 2. 设计评审

- **幂等 upsert（关键决策）**：新增 `upsertClassNode`/`upsertPropertyNode`/`upsertMetricNode`，用 `MERGE (n:Label {id}) SET ... RETURN n` 建则建、有则改。seed 重跑不产生重复节点，update 在节点缺失时也能兜底创建。
- **关系重建用 reconcile（关键决策）**：`updateProperty` 检测到 `is_foreign_key`/`ref_class_id` 变化、`updateMetric` 检测到 `target_class_id` 变化时，调用 `reconcilePropertyReferences`/`reconcileMetricDerivedFrom`——先 `MATCH (n)-[r:REL]->() DELETE r` 再按新目标 `MERGE`，保证图上恰好一条边、无残留旧边。仅当关系相关字段被修改时才触发，避免每次 update 都重建。
- **best-effort 不阻断 PG（沿用既有模式）**：Neo4j 同步包在 try/except 内仅记 warning，PG 更新始终成功；与 create/delete 的既有行为一致。
- **seed 的 pid 追踪**：seed 需在属性创建后拿到 PG id 才能建 Neo4j 关系，故在创建/复用两条路径下都记录 `(class_id, property_name) -> property_id`。
- **KISS**：不引入批处理/图查询接口，仅补齐"同步"职责。

## 3. 数据模型变更

无新增表/列。变更的是 Neo4j 图：

- 新增 `:Class`（19 个，属性 id/name/alias/description/sourceTable）
- 新增 `:Property`（491 个，属性 id/name/alias/dataType/sourceColumn/isPrimaryKey/isForeignKey）
- 关系 `(:Class)-[:HAS_PROPERTY]->(:Property)`（491 条）
- 关系 `(:Property)-[:REFERENCES]->(:Class)`（35 条，外键）

## 4. 接口契约变更

无 API 契约变化。`updateClass`/`updateProperty`/`updateMetric` 响应体不变，仅内部增加 Neo4j 双写。

## 5. 实现要点

- `app/infrastructure/neo4j_client.py`：新增 `upsertClassNode`/`upsertPropertyNode`/`upsertMetricNode`（MERGE+SET 幂等）、`reconcilePropertyReferences`/`reconcileMetricDerivedFrom`（DELETE 旧边 + MERGE 新边）。均参数绑定，无字符串拼接注入面。
- `app/services/ontology_service.py`：三个 update 方法在 PG commit+refresh 后 best-effort 调用 upsert；关系字段变化时触发 reconcile。
- `seed_ontology.py`：属性循环记录 `pid` 映射；新增 `_syncToNeo4j(cid, pid)` 幂等同步全部类/属性/关系；`seed()` 结尾调用，失败仅告警（可重跑补齐）。

## 6. 测试

- `test_ontology_api.py` 新增 8 例：updateClass 同步、updateClass 容忍 Neo4j 失败、updateProperty 同步、updateProperty 重建 REFERENCES、updateProperty 在 `is_foreign_key=False` 时清除残留 REFERENCES 边、updateProperty 幂等自愈 HAS_PROPERTY 缺失边、updateMetric 同步、updateMetric 重建 DERIVED_FROM。
- `test_seed_ontology_sync.py`（新增，5 例）：upsert 全部 19 类、upsert+关联全部 491 属性且与 pid 一致、外键 REFERENCES 指向合法目标类、Neo4j 失败容忍、seed() 真实内存 SQLite 全量跑通并触发 `_syncToNeo4j`（cid 19 个、pid 覆盖全部属性）。
- `test_neo4j_client.py`（新增，4 例）：`deleteNode` label 白名单守卫拒绝非法标签（含注入样本）、接受合法标签、URI 日志脱敏。
- 全量：**273 passed**，覆盖率 **88.32%**（≥80% 门槛）。

## 7. 安全审查

`python-reviewer` + `security-reviewer` 各 0 CRITICAL。python-reviewer 4 个 HIGH 已全部修复：

| 级别 | 问题 | 修复 |
|------|------|------|
| HIGH | `deleteNode` 用 `%s` 字符串拼接 label，存在 CQL 标签注入面 | 新增 `_ALLOWED_LABELS = frozenset({"Class","Property","Metric"})` 白名单守卫，非白名单立即 `ValueError` |
| HIGH | create 阶段中断导致 HAS_PROPERTY 缺失边无法自愈 | `updateProperty` 无条件调用幂等 `linkClassHasProperty(entity.class_id, entity.id)` |
| HIGH | seed 全量 sync 包在单一 try/except，一个属性失败放弃其余 | 逐类/逐属性独立 try/except，单个失败跳过该条并告警 |
| HIGH | `is_foreign_key=False` 但 `ref_class_id` 残留时 REFERENCES 边仍在 | reconcile 目标绑定到 `is_foreign_key` flag，为 False 时仅删旧边不建新边 |

记录为 follow-up（非 #65 文件、改动前已存在，未获授权不改）：
- security-reviewer HIGH：`config.py` 硬编码 `DATABASE_URL`/`SECRET_KEY` 默认值（建议启动时校验必填、默认不携带凭据）。
- security-reviewer M2：Neo4j 默认凭据（`qa_neo4j_dev_2026`）内置于 Settings 默认值。
- security-reviewer L1：`OntologyMetric.formula` 无 `max_length` 约束。
- security-reviewer L2：`_DRIVER` 单例非线程安全（async 场景下并发 getDriver 可能重复建连接；当前最佳实践够用，建议后续加锁）。

## 8. 部署验证

- 真实 Postgres + 真实 Neo4j（docker 容器运行中）跑 `seed_ontology.py`：
  - PG：19 类存在、491 属性跳过（幂等复用）。
  - Neo4j 落图：19 Class / 491 Property / 491 HAS_PROPERTY / 35 REFERENCES；ItemMaster 挂 30 属性。
  - `notifications_min_severity="WARNING"` 抑制了 MERGE 双模式产生的 INFO cartesian-product 通知，seed 输出无刷屏噪音。
- 真实 API（port 8000）更新类 id=1：`classAlias`/`description` 变更后 Neo4j 节点属性一致更新。
- 真实 API 更新属性 id=200 的 `refClassId`（BPSUPPLIER→BPARTNER）：Neo4j 上 REFERENCES 恰好一条边指向新目标类，旧边被清除；随后恢复原外键，边回指 BPSUPPLIER。
- **最终对账（PG↔Neo4j 全量）**：写脚本比对两侧 `ontology_property.class_id`/`ref_class_id` 与图上 HAS_PROPERTY/REFERENCES 边。发现唯一异常：prop 200 存在一条来自 Customer（class 1）的多余 HAS_PROPERTY 边（PG 归属为 YPRECEIPT/class 10）。已确认为真实环境早期 API 调试残留的数据异常（非代码路径——seed 与 updateProperty 的 class id 均来自 PG），在图上删除该边后复跑对账：491 个 property 各恰好 1 条 HAS_PROPERTY 边，35 条 REFERENCES 与 PG 外键定义一一对应，无缺失无多余。

## 9. 关联

- 设计稿：`docs/设计01-架构蓝图.md`、`docs/设计02-详细设计.md`（Phase 2 本体管理）
- Wiki：`Harness/wiki/data-model.md`
- 前置：`Harness/changes/feat-phase2-ontology/`（若存在）
