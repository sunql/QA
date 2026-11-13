# 指标/KPI 纳入向量图管理（Milvus + Neo4j 同步与召回消费）设计稿

> 日期：2026-11-13
> 状态：🟢 APPROVED（2026-11-13 设计评审通过）
> 基线：`main` @ `4a0ed515`
> 上游设计：`docs/superpowers/specs/2026-11-11-kpi-definition-consumption-design.md`（K1-K6/P0-P5）
> 批次定位：KPI 剩余三块工作（①同步能力 → ②P5 治理收口 → ③P4b 编译器）中的**子项目①**
> 约束：不部署生产、不改生产库；本地 Milvus/Neo4j 测试套件禁跑（19530/7687 是生产）。

## 1. 背景与现状修正

用户诉求：新建的指标与 KPI 目录数据能像本体类/属性一样被向量化、图管理、被大模型召回使用。

现状实测（2026-11-13 调研）：

| 对象 | Milvus | Neo4j | 缺口 |
|---|---|---|---|
| ontology_class / ontology_property | ✅ 3 collection 之一，CRUD 钩子齐全 | ✅ Class/Property 节点，钩子+全量对账 | — |
| **ontology_metric** | ✅ `ontology_metric_embeddings`，CRUD 钩子齐全 | ✅ `Metric` 节点，仅钩子路径 | ① 不参与 NL2SQL 类召回（`chat_recall.py:498` 固定 `typeFilter="class"`）；② `syncMissingGraph` 全量对账不覆盖 Metric 节点（`ontology_service.py:1837-1859`）；③ **回填脚本 bug**：`scripts/backfill_milvus_embeddings.py --cleanup` drop 全部 3 集合却只写回类/属性 ⇒ 一跑清空指标向量 |
| **kpi_catalog** | ❌ 完全在外（模型文档明确排除） | ❌ 无节点 | 无 collection、无 unified_id/id_mapping 注册、无同步钩子 |

KPI 语义匹配现状：纯文本（DB 缓存 + 子串包含式），无向量检索；匹配缓存只收 PUBLISHED（`kpi_match_cache.py:175`）。

## 2. 已裁决决策（用户确认）

| # | 决策 | 结论 |
|---|---|---|
| D1 | 批次顺序 | 同步能力 → P5 治理收口 → P4b 编译器（P4b 的 YAGNI 裁决暂不推翻，届时再裁） |
| D2 | 范围 | 全量：存储 + 消费 + 排雷（向量候选进 chat 匹配链路） |
| D3 | 状态口径 | KPI 向量只索引 PUBLISHED（与匹配缓存同口径）；metric 维持现状（DRAFT 也建向量） |
| D4 | 触发机制 | CRUD best-effort 钩子（沿用现有模式）+ syncMissing 全量对账兜底 + admin 手动端点 + 每日 reconcile 计数扩展；**不引入 outbox 可靠队列**（避免与老路径双轨） |
| D5 | 手工入口界面 | KPI 目录页（全量同步按钮 + 行级重新同步）+ 本体类 Tab 既有按钮能力升级（不加新按钮）；诊断页只展示同步健康，不放同步按钮 |

## 3. 存储面

### 3.1 Milvus（kpi_catalog 新增）

- 新 collection `kpi_catalog_embeddings`，schema 与 ontology 三集合同形：
  `ontology_id`（= KPI id 字符串）、`type`（固定 `"kpi"`）、`name`、`alias`、`description`、`embedding`（FLOAT_VECTOR）、`external_id`（= kpi code）。
- embedding 文本 = `kpi_name + aliases + semantic_keywords + description + unit`。
- 复用 `infrastructure/milvus_dual_write.py` 的 ensure/insert/delete 路由（新增 `"kpi"` 分支 + `ensureKpiCollection`）。

### 3.2 Neo4j（kpi_catalog 新增）

- 新 label `KpiCatalog`，MERGE 键 = `unified_id`；节点属性：code、name、status、owner 等。
- 关系：`(:KpiCatalog)-[:MEASURED_BY]->(:Metric)`（经 `kpi.metric_id`，metric 缺失时不建边）。

### 3.3 id_mapping

- KPI 注册 `business_object="KPI"`，unified_id = `obj:KPI:{id}`，复用 A1 ③ 的 id_mapping sync bootstrap。
- 存量 12 条 PUBLISHED KPI 一次性回填（脚本，幂等）。

### 3.4 状态语义

- KPI 向量：只索引 PUBLISHED；状态流转到 DRAFT/DEPRECATED ⇒ 从向量删除（节点保留，带 status 属性）。
- metric 向量/图：维持现状，不变更。

## 4. 同步机制（钩子 + 对账兜底）

- 新独立文件 `app/services/kpi_vector_graph_sync.py`（镜像 `_syncMetricEmbeddingBestEffort` 模式）：
  `kpi_catalog_service` 的 create/update/delete 提交后挂 best-effort 钩子；状态流转到 PUBLISHED 额外触发向量写入。
- **对账兜底补三缺口**：
  1. `syncMissingGraph` 补 Metric 节点修复 + KpiCatalog 节点/MEASURED_BY 边；
  2. 新 admin 端点 `POST /api/v1/kpi-catalog/embeddings/sync-missing`（全量对账：删孤儿/补缺失，向量+图）与 `POST /api/v1/kpi-catalog/{id}/embedding`（单条重建）；
  3. 每日 `knowledge_reconcile_worker` 计数比对扩展 kpi 向量数/图节点数键。
- Milvus delete 可见性照既有约定：CRUD 删除走 delete+flush；对账/单条重建路径按需 release+load。

## 5. 排雷（既有 bug 修复，无行为开关）

- `scripts/backfill_milvus_embeddings.py`：
  `_backfill` 补 metric + kpi 段；`--cleanup` 与 rebuild 的期望集合覆盖**全部 4 个** collection；更正「metric 只删不插」过期注释。

## 6. 消费面（向量候选进匹配器）

- `kpi_semantic_match_service` 增加可选向量候选步：
  问题 embedding → 检索 `kpi_catalog_embeddings` topK + `ontology_metric_embeddings` topK（metric 命中经 `kpi.metric_id` 反查绑定 KPI）→ 以 `match_method="vector"` 并入候选集。
- 安全规则：
  - 包含式命中仍优先（最长匹配赢，向量候选只补空白）；
  - 向量候选有距离阈值上限（超限丢弃）；
  - 单强命中 ⇒ 按常规路由；多命中/低置信差 ⇒ 走既有澄清流，**不猜**；
  - Milvus 不可用 ⇒ 静默回落纯文本匹配，不新增 chat 故障面。
- 开关 `KPI_MATCH_VECTOR_ENABLED` **默认关**（灰度）。
- metric 向量召回**不进** NL2SQL schema 裁剪（类召回 `typeFilter="class"` 不动），仅作匹配器候选源——避免 schema 列回声冗余。

## 7. 手工入口（界面落点，D5）

| 界面 | 动作 | 说明 |
|---|---|---|
| **KPI 目录页**（`frontend/src/pages/KpiCatalogPage.tsx`） | 页头「向量/图同步」按钮 → `POST /kpi-catalog/embeddings/sync-missing`；列表行操作「重新同步」→ `POST /kpi-catalog/{id}/embedding` | 仅 admin 可见 |
| **本体管理页 · 类 Tab**（`frontend/src/components/ontology/ClassTab.tsx`） | **不加新按钮**；「图谱对账」背后接口升级后自动覆盖 Metric 节点 | 界面不变、能力扩大 |
| 管理诊断页 | 只增同步健康**展示**（kpi 向量数 vs PG PUBLISHED 数、图节点数） | 只读，不放同步按钮 |

## 8. 可观测性

- `GET /api/v1/admin/kpi/diagnose` 增补：向量候选命中（distance / match_method）、同步健康字段。
- routing 指标：`match_method="vector"` 落既有 routing 字段，不新增面板。

## 9. 测试策略

- 纯单测：embedding 文本构造、候选合并/阈值/澄清边界、状态流转删向量语义。
- 真 PG 集成：id_mapping 回填、对账端点、diagnose 字段（Milvus/Neo4j 用 fake double——本地禁跑真实向量图套件）。
- 防假绿：候选合并与阈值守卫做双向断言（正例命中 + 反例超限丢弃）；断言 `match_method` 值本身而非仅命中数。
- 部署后人工冒烟：本地栈验证 KPI 新建→向量/图出现→诊断接口可见（对齐 P4a 惯例）。

## 10. 开关与回滚

| 开关 | 默认 | 关闭效果 |
|---|---|---|
| `KPI_MATCH_VECTOR_ENABLED` | off | 消费回退纯文本匹配（存量行为零变化） |
| KPI 同步钩子开关（随现有 switches 模式） | on | 停止增量同步；存量靠 sync-missing 手动兜底 |
| 排雷修复 | — | 无开关，纯正确性修复 |

## 11. 验收标准

1. 新建 KPI（admin）→ 保存后向量/图节点自动出现（钩子路径）；diagnose 可见。
2. KPI 转回 DRAFT → 向量消失、图节点保留且 status 更新。
3. `sync-missing` 手动端点幂等：复跑 changes=0；对 Metric 图节点与 kpi 向量能修复缺失。
4. 回填脚本 `--cleanup` 后 4 个 collection 全部恢复（修复前会清空 metric）。
5. 灰度开 `KPI_MATCH_VECTOR_ENABLED` 后：语义相近问句（不含精确关键词）可命中 KPI（match_method=vector）；歧义走澄清；关开关即回退。
6. 全部新增/改动测试净增红为 0。

## 12. 非目标

- ❌ outbox/worker 可靠队列改造（D4 裁决不引入）
- ❌ 类/属性老同步路径迁移重构
- ❌ metric 向量的 DRAFT 状态口径变更
- ❌ P5 发布闸门/看板（子项目②）、P4b 编译器（子项目③）
- ❌ NL2SQL schema 裁剪消费 metric 向量
