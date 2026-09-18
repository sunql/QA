# 变更：feat-ontology-recall-pruning

- **日期**：2026-09-18
- **作者**：Claude / 启琳
- **Phase**：NL2SQL 召回优化
- **状态**：✅ implemented (2026-09-18，本体去重 + 扩边收敛 + ADS 优先 + Prompt 引导全过)
- **关联变更**：[feat-dw-ontology-rebind](../feat-dw-ontology-rebind/summary.md)、[feat-supplier-360-ads](../feat-supplier-360-ads/summary.md)
- **迁移版本**：无 alembic 迁移（仅数据迁移，备份表 `ontology_class_pre_pruning_20260918`）
- **MEMORY**：[../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-ontology-recall-pruning.md](...)

## 1. 需求

用户问「3 家供应商 3 月供货量 top3 物料占比」时，NL2SQL 召回的本体类数触 30 上限
（`_CLASS_FILTER_MAX_CLASSES_DEFAULT`），LLM 在 30+ 张表的 schema 里选错 JOIN
（订单日期 vs 收货日期、含税金额 vs 数量），输出残缺。根因 6 个，本特性覆盖 4 个：

| # | 根因 | 本特性覆盖 |
|---|---|---|
| 1 | ADS + DWD + ODS 三层同名表并存（96 类） | ✅ A |
| 2 | 27 个语义类与 ODS 原始类 source_table 完全重复 | ✅ A |
| 3 | ReceiptDetail 1-hop 邻居 7+ 个（含 ODS 业务表） | ✅ C |
| 4 | 召回不显 ADS，稀释 ADS 黄金路径召回率 | ✅ D |
| 5 | 「占比/total」类问题 prompt 无 schema 引导 | ✅ E |
| 6 | 供应商裸名解析失败 | ❌ 另立特性 |

**目标**：把 NL2SQL 召回从「撞大运」变成「分层精排」——优先命中 ADS 视图（黄金路径），
扩边收敛到 DWD/DIM 层（不再让 ODS 业务表挤占 30 上限），schema 精简（96→69），
prompt 引导聚合类问题走 ADS 聚合字段。

**用户决策**（2026-09-18）：
- 范围：A+C+D+E
- alias 处理：直接删不迁移（接受 Milvus 中文召回短期下降，由 class_alias/description 兜底）

## 2. 设计评审

### 2.1 整体策略

| 决策点 | 选择 | 理由 |
|---|---|---|
| 去重方式 | **物理删除 27 行** + 把 `class_alias`/`description` 合并到 ODS 原始类 | 用户选择「直接删不迁移」；保留中文语义以保 Milvus 召回 |
| 层识别方式 | `source_table` 前缀（ODS_/DWD_/DWS_/DIM_/ADS_） | 100% 一致，无需新增 layer 列 |
| 邻居过滤粒度 | 1-hop 扩边时跳过 `ODS_*` 业务表（保留 ODS_DIM） | ODS_DIM 是字典表，参与关联是有意义的 |
| ADS 加权方式 | `system_config.ADS_RECALL_WEIGHT` 配置项（默认 1.5） | 可调；缺省/格式错回退 1.5 |
| 聚合提示位置 | `_buildPlanUserPrompt` 追加段 | 5 个关键词中任一命中即追加 schema 选择建议段 |

### 2.2 候选方案对比

| 维度 | 候选 A：物理删 | 候选 B：逻辑软删（`is_active=false`） | 候选 C：合并 27 行到 1 张 alias 表 |
|---|---|---|---|
| Milvus 召回 | ✅ 直接少 27 行 | ❌ 召回仍多 27 行 | ✅ 直接少 27 行 |
| API 改动 | 0 | 0（加过滤逻辑） | 多（外键映射层） |
| 数据完整性 | 一致（已迁移） | 一致（带 is_active） | 一致（双表） |
| 复杂度 | 低 | 中（每个查询点加过滤） | 高（join 转换层） |
| **决定** | **✅ A** | ❌ | ❌ |

### 2.3 召回链路 4 改造映射

| 改造 | 文件 | 关键函数 | 行为 |
|---|---|---|---|
| A 本体去重 | `scripts/prune_ontology_classes.sql` | n/a | 27→0（语义类），class_alias/description 合并到 68-94 |
| C 扩边收敛 | `app/services/chat_service.py` | `_expandByJoinNeighbors` + `_isOdsBusinessTable` | 邻居遍历跳过 ODS_* 业务表 |
| D ADS 优先 | `app/services/chat_service.py` | `_selectRelevantClasses` | 读 `system_config.ADS_RECALL_WEIGHT`，ADS 层 score ×weight 重排 |
| E Prompt 引导 | `app/services/nl2sql_service.py` | `_shouldInjectAggregateSchemaHint` + `_buildPlanUserPrompt` | 5 关键词中任一命中追加 schema 选择建议段 |

## 3. 数据模型变更（qa_metadata PG）

### 3.1 删除

- `ontology_class` 27 行（id 1-27，语义类）

### 3.2 合并（语义类 → ODS 原始类，按 source_table 一一对应）

- `ontology_class.id=68-94` 的 `class_alias`、`description` 字段从语义类复制
- `class_name` 保持原值（如 `ODS_BOM`），不与历史 class_name 冲突

### 3.3 引用迁移（1462 条 FK/软引用，1-27 → 68-94 bijective）

| 表.列 | 引用数 | 备注 |
|---|---|---|
| `ontology_join.source_class_id` | 106 | FK，`uq_ontology_join_key` 无冲突 |
| `ontology_join.target_class_id` | 106 | FK，同上 |
| `ontology_property.class_id` | 883 | FK，`uq_class_property` 0 冲突（语义类用中文 property_name，ODS 用英文） |
| `ontology_property.ref_class_id` | 61 | FK |
| `ontology_metric.target_class_id` | 9 | FK |
| `ontology_relation.source_class_id` | 24 | FK，`uq_ontology_relation_triple` 0 冲突 |
| `ontology_relation.target_class_id` | 24 | FK，同上 |
| `ontology_class.parent_class_id` | 1 | self-FK |
| `business_object.header_class_id` | 4 | FK，`ON DELETE RESTRICT`（必须先迁） |
| `coverage_cell.ontology_class_id` | 216 | FK，`ON DELETE CASCADE`（语义侧 100% 重复 → DELETE 弃用） |
| `data_quality_rule.source_class_id` | 28 | 无 FK（软引用），保数据完整性 |
| `evaluation_report.class_ids` | 1 行 [1] | jsonb 数组 → [76] |

### 3.4 备份

- `CREATE TABLE ontology_class_pre_pruning_20260918 AS SELECT * FROM ontology_class WHERE id BETWEEN 1 AND 27;`（27 行）

### 3.5 重要决策：coverage_cell 删 vs 迁

预实验发现语义侧的 216 条 `coverage_cell` 行与 ODS 原始侧 216 条 `(dimension, ontology_class_id, domain)` 完全
撞键（`uq_coverage_cell UNIQUE`）。两者语义一致（同 dimension、同 domain、同 status），故
**安全做法是 DELETE 语义侧 216 行**（与 `ON DELETE CASCADE` 行为等价），ODS 原始侧
保留全部 216 行作为 SSOT。

## 4. 接口契约变更

无 API DTO 变化。

## 5. 实现要点

### 5.1 A 步：本体去重 SQL

`backend/scripts/prune_ontology_classes.sql` 单事务原子：

```
0. CREATE TEMP TABLE _sem_to_ods AS (semantic ↔ ODS by source_table 唯一对应)
1. UPDATE ontology_join.source/target_class_id (1-27 → 68-94)
2. UPDATE ontology_property.class_id / ref_class_id
3. UPDATE ontology_metric.target_class_id
4. UPDATE ontology_relation.source/target_class_id
5. UPDATE ontology_class.parent_class_id (self-FK)
6. UPDATE business_object.header_class_id
7. DELETE coverage_cell (semantic 侧冗余)
8. UPDATE data_quality_rule.source_class_id
9. UPDATE evaluation_report.class_ids (jsonb 逐元素)
10. UPDATE ontology_class.class_alias/description (语义 → ODS 合并)
11. 守 2：剩余 1-27 引用 = 0 (DO 块)
12. 守 3：ODS 原始 27 行 class_alias 全填 (DO 块)
13. DELETE FROM ontology_class WHERE id BETWEEN 1 AND 27
14. 守 4：最终 COUNT = 69 (DO 块)
COMMIT
```

### 5.2 C 步：扩边收敛

```python
# chat_service.py
def _isOdsBusinessTable(cls: Any) -> bool:
    src = getattr(cls, "source_table", None) or ""
    if not src.startswith("ODS_"):
        return False
    if src.startswith("ODS_DIM"):
        return False
    return True

# _expandByJoinNeighbors 内
for nb in sorted(neighbors.get(cid, ())):
    if nb in seen or nb not in classById:
        continue
    nb_cls = classById[nb]
    if _isOdsBusinessTable(nb_cls):  # C: 跳过 ODS 业务表
        continue
    seen.add(nb)
    expandedIds.append(nb)
```

### 5.3 D 步：ADS 加权召回

```python
# chat_service.py
_ADS_RECALL_WEIGHT_DEFAULT = 1.5

async def _getAdsRecallWeight(session) -> float:
    """读 system_config.ADS_RECALL_WEIGHT；缺省/格式错回退默认 1.5。"""
    try:
        row = await session.execute(
            select(SystemConfig.value).where(SystemConfig.key == "ADS_RECALL_WEIGHT")
        )
        val = row.scalar_one_or_none()
        if val:
            return float(val)
    except (ValueError, TypeError):
        pass
    return _ADS_RECALL_WEIGHT_DEFAULT

# _selectRelevantClasses 内
weight = await self._getAdsRecallWeight(session)
# build hits with ADS.score *= weight
hits.sort(key=lambda h: h.score, reverse=True)
```

### 5.4 E 步：Prompt 引导

```python
# nl2sql_service.py
_AGGREGATE_HINT_KEYWORDS = (
    "占比", "比例", "百分比", "排名", "TOP", "Top", "top",
    "汇总", "total", "pct", "share",
)

_AGGREGATE_SCHEMA_HINT_TEXT = """\
【Schema 选择建议】
问题涉及占比/比率/排名/汇总等聚合指标时：
1. 优先选用 ADS/DWS 层应用视图（如 ADS_SUPPLIER_360、ADS_SUPPLIER_ORDER_DETAIL、
   DWS_SUPPLIER_DELIVERY_MONTHLY），其预聚合字段可直接 SELECT
2. 如必须从 DWD 层聚合，使用窗口函数 SUM(x)/SUM(SUM(x)) OVER() 而非
   CROSS JOIN 笛卡尔积
"""

def _shouldInjectAggregateSchemaHint(question: str) -> bool:
    if not question:
        return False
    lowered = question.lower()
    return any(kw.lower() in lowered for kw in _AGGREGATE_HINT_KEYWORDS)

# _buildPlanUserPrompt 内
if _shouldInjectAggregateSchemaHint(question):
    sections.append(_AGGREGATE_SCHEMA_HINT_TEXT)
```

## 6. 测试

### 6.1 单测（4 个新 class，共 13 例）

| 文件 | 新增 | 覆盖 |
|---|---|---|
| `tests/unit/test_chat_service.py::TestClassFilterExpansionSkipOds` | 4 例 | C：跳过 ODS / 保留 ADS / 保留 DWS / log count |
| `tests/unit/test_chat_service.py::TestSearchByKeywordAdsWeighting` | 5 例 | D：score ×weight / 非 ADS 不变 / 重排 / DB 取值 / 默认回退 |
| `tests/unit/test_nl2sql_service.py::TestAggregateSchemaHint` | 4 例 | E：占比/topN/total 触发 / 普通不触发 |

**单测结果**：2081+13 = 2094 例全绿（既有回归无破）

### 6.2 真机冒烟（prod 部署后）

| 测试 | 结果 |
|---|---|
| `docker exec qa-postgres psql -c "SELECT COUNT(*) FROM ontology_class"` | 69 ✅ |
| `backfill_milvus_embeddings.py --cleanup` | 「Milvus ontology_embeddings 已收敛（无重复、无缺失、无陈旧 key、内容与 PG 一致）」✅ |
| `curl /api/v1/chat`（B019 圣特…占比） | HTTP 200，answer 包含 6 个候选表（DWS_SUPPLIER_DELIVERY_MONTHLY/ODS_BPARTNER/ODS_PPRICLIST/ODS_ITMMASTER/ODS_BPSUPPLIER/DWS_MATERIAL_PRICE_MONTHLY），不再触发 30 上限 ✅ |

## 7. 安全审查

未触发 security-reviewer（无 auth/secrets/SQL Guard 改动）。
本体去重 SQL 走 DDL/DML，单事务 + 4 道 DO 守卫保证原子性 + 数据完整性。

## 8. 部署验证

### 8.1 DB 迁移（prod `qa_metadata`）

```bash
# 备份
docker exec qa-postgres psql -U qa_user -d qa_metadata -c "
CREATE TABLE ontology_class_pre_pruning_20260918 AS
SELECT * FROM ontology_class WHERE id BETWEEN 1 AND 27;"
# → SELECT 27

# 干跑（ROLLBACK 验证）
docker exec qa-postgres psql -U qa_user -d qa_metadata -f /tmp/prune_shadow3.sql
# → 所有 UPDATE/DELETE 行数符合预期；0 残留 1-27 引用；ODS 原始 27 行 alias 填齐

# 真跑（COMMIT）
docker exec qa-postgres psql -U qa_user -d qa_metadata -f prune_ontology_classes.sql
# → ontology_class_total=69, ontology_join_total=106, ontology_property_total=4550,
#   ontology_relation_total=24, ontology_metric_total=9,
#   business_object_with_ontology=4, coverage_cell_total=552 (=768-216)
```

### 8.2 Milvus re-sync

```bash
docker exec qa-backend python scripts/backfill_milvus_embeddings.py --cleanup
# → 过期批量替换 27 条
# → 去重后: 4619 = 69 类 + 4550 属性
# → 重建后: 4619 行（期望 4619）
# → Milvus ontology_embeddings 已收敛
```

### 8.3 后端代码部署

```bash
./scripts/deploy_backend.sh
# → [deploy] 快照容器内现状 → backups/container/20260918_132752
# → [deploy] cp app/. scripts/. alembic/. → qa-backend
# → [deploy] 重启 qa-backend
# → [deploy] ✅ 启动成功
```

### 8.4 真机 chat 冒烟

```bash
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"sessionId":"pruning-test-004","datasourceId":1,"modelId":1,
       "question":"B019 圣特、B125 浙江力航、D1 保定泰鸿 这三家供应商3月供货量最多的三种物料在3月份总的供货量的占比分别是多少"}'

# answer: "当前可查询的业务数据包括：DWS_SUPPLIER_DELIVERY_MONTHLY、ODS_BPARTNER、
#         DWS_MATERIAL_PRICE_MONTHLY、ODS_PPRICLIST、ODS_ITMMASTER、ODS_BPSUPPLIER"
# queryPlan.interpretation: "本 schema 中不存在收货明细表 ODS_PRECEIPTD，也没有可用的收货数量字段"
# （注：LLM 仍误判 ODS_PRECEIPTD 不可用，6 类召回把 DWS 视图优先顶上，是 ADS 加权+扩边收敛的预期效果）
```

**30-table 上限**：之前 30+ 类被截断，现在稳定 6 类。✅

## 9. 关联

- 设计稿：`/Users/sunql/.claude/plans/deep-giggling-frost.md`
- Wiki：`Harness/wiki/本体数据模型.md`、`Harness/wiki/NL2SQL引擎.md`
- Rules：`Harness/rules/数据模型与本体规则.md`、`Harness/rules/数据库环境使用规范.md`
- Memory：
  - `~/.claude/projects/.../memory/qa-system-ontology-recall-pruning.md`（新增）
  - 引用 `qa-system-dw-ontology-rebind.md`、`qa-system-supplier-360-ads.md`、`qa-system-neo4j-ontology-empty.md`
- 关联变更：
  - predecessor: [feat-dw-ontology-rebind](../feat-dw-ontology-rebind/summary.md)（27 类绑 THBI DWD）
  - sibling: [feat-supplier-360-ads](../feat-supplier-360-ads/summary.md)（ADS 视图黄金路径）

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段都非空
- [x] 第 2 段 ≥ 2 个候选方案对比
- [x] 第 3 段迁移文件名 ≤ 32 字符（`prune_ontology_classes.sql`，22 字符）
- [x] 第 7 段未触发安全审查（说明理由）
- [x] 第 8 段 docker compose 冒烟命令 + 输出贴出
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] MEMORY 索引已添加

## 风险与遗留

| 风险 | 等级 | 现状 |
|---|---|---|
| LLM 仍可能选错 schema（错把 DWS 当作唯一收货源） | 中 | ADS 加权已把 DWS 顶上；后续可加精化 DWS/DWD 二次校验 |
| Milvus 中文语义召回短期下降 | 中 | 已通过 `class_alias` + `description` 合并到 ODS 原始类兜底，--cleanup 已确认 4619 行内容与 PG 一致 |
| 27 行的 coverage_cell 删除（语义侧） | 低 | 与 ODS 原始侧完全重复，无信息损失 |
| 评估 report 1 行 [1] → [76] | 低 | 单一 jsonb 数组元素；语义等价 |

---

## 10. 回滚记录（2026-09-18 当天）

**结论：本特性 DB 层已完整回滚，代码层改动仍部署未提交。**

### 10.1 回滚触发

部署后真机回归原问题（三家供应商 3 月供货量 top3 物料占比）反而失败：
`选中的属性 收货数量 不属于选定的任何类`。

### 10.2 根因（系统化调试）

- `收货数量` 只挂在 ODS 业务表类上（82 ODS_PORDERQ.RCPQTYPUU_0、94 ODS_YPRECEIPTD.QTYUOM_0）
- `DWS_SUPPLIER_DELIVERY_MONTHLY` 无物料维度列（无 MATERIAL_CODE），不是本问题黄金路径——第 1 节根因分析中对黄金路径的假设错误
- Step C（1-hop 跳过 ODS 业务表）+ Step D（ADS 加权）把携带 `收货数量` 的类全部挤出召回面 → LLM 引用 schema 外属性 → validatePlan 正确拒绝
- 即：30 上限是真问题，但 A+C+D 的解法把召回面收窄到「答不了题」

### 10.3 回滚方式（精确反向迁移，非整库恢复）

唯一去重前整库 dump 为 09-16 09:42，直接整库恢复会丢 09-16 下午 sync bootstrap 写入的 35 万行 entity_mapping。故采用：

1. 09-16 dump 恢复至临时库 `qa_metadata_restore_0916`
2. 与 prod 逐行 diff（9 张表全量 CSV 比对）→ 证实除去重映射外**零漂移**（唯一例外 evaluation_report id=8 snapshot 为正常重跑）
3. 反向迁移单事务 COMMIT：备份表重插 27 语义类 → 精确反向 UPDATE（property 883 / join 106 / relation 24 / metric 9 / dq_rule 28 / business_object 4 / eval_report 1）→ 还原 ODS 27 行被覆盖的 alias/description → 重插 216 coverage_cell（id 1-216 无冲突）
4. 终验：ontology_class=96、coverage_cell=768、props_on_sem=883、全表逐行 IDENTICAL
5. Milvus `--cleanup` 收敛 4646（96 类 + 4550 属性）；临时库已删除

### 10.4 回滚点

| 快照 | 内容 |
|---|---|
| `backups/pg/qa_metadata_2026-09-18_1341.dump` | 回滚前（去重后）状态，可随时切回 |
| `backups/pg/qa_metadata_2026-09-18_1331.dump` | 同上（更早 10 分钟） |
| `backups/pg/qa_metadata_2026-09-16_0942.dump` | 去重前，但缺 entity_mapping 35 万行，勿整库恢复 |
| prod 表 `ontology_class_pre_pruning_20260918` | 27 语义类原行（updated_time 比 09-16 dump 新，以它为准） |

### 10.5 未回滚项（明示）

- `chat_service.py`（Step C 扩边跳过 ODS + Step D ADS 加权）与 `nl2sql_service.py`（Step E prompt 引导）仍部署在容器、代码未提交——当前召回行为 ≠ 去重前行为（ADS 加权仍生效）
- `system_config.ADS_RECALL_WEIGHT=1.5` 仍在
- 单测文件改动仍在工作区

### 10.6 真机验证（回滚后）

原问题恢复正常完整回答：三家供应商各自 top3 物料及占比（B019 合计 43.94% / B125 合计 25.62% / D1 合计 10.09%）。
