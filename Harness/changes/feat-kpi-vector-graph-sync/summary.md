# 指标/KPI 纳入向量图管理（子项目①）

- 分支：`feat/kpi-vector-graph-sync`（worktree `.worktrees/feat-kpi-vector-graph-sync`），base = main `b6b384e5`
- 设计稿：`docs/superpowers/specs/2026-11-13-kpi-vector-graph-sync-design.md`
- 实施计划：`docs/superpowers/plans/2026-11-13-kpi-vector-graph-sync.md`
- 状态：**代码完成、评审通过、未合并、未部署、生产库未动**

## 背景

用户诉求：新建的指标（ontology_metric）与 KPI 目录数据能像本体类/属性一样被向量化、进图管理、被大模型召回使用。

现状（设计稿 §1 实测）：

| 对象 | Milvus | Neo4j | 缺口 |
|---|---|---|---|
| ontology_class / ontology_property | ✅ 三 collection 之一，CRUD 钩子齐全 | ✅ Class/Property 节点 + 钩子 + 全量对账 | — |
| ontology_metric | ✅ `ontology_metric_embeddings`，CRUD 钩子齐全 | ✅ `Metric` 节点，仅钩子路径 | 全量对账不覆盖 Metric 节点；回填脚本 `--cleanup` 会清空 metric 向量 |
| kpi_catalog | ❌ 完全在外 | ❌ 无节点 | 无 collection / 无 id_mapping / 无同步钩子 |

本批把 KPI 目录纳入同一套存储面（Milvus `kpi_catalog_embeddings` + Neo4j `KpiCatalog` 节点与
`MEASURED_BY` 边），并提供钩子、全量对账兜底、admin 手动入口、每日计数比对与向量召回消费。

## 变更清单

### 逐 commit（`git log --oneline b6b384e5..HEAD`，23 个）

| commit | 说明 |
|---|---|
| `9104500b` | docs: 修复计划 Task 10 多余围栏（围栏总数 182 偶数，Task 11/12 标题恢复可提取） |
| `c389f0e6` | feat: Milvus 新增 kpi_catalog_embeddings type-routed collection（external_id 透传） |
| `1ffec754` | fix: KPI 集合缺失时读路径优雅降级（可选集合探针 + 双向守卫测试） |
| `5910c784` | feat: Neo4j KpiCatalog label + MEASURED_BY 边 + unified_id 读数 |
| `12009bd5` | feat: kpi_vector_graph_sync 同步核心（向量 PUBLISHED 口径 + 图全状态 + MEASURED_BY 边集合替换 + best-effort） |
| `2573e257` | fix: KPI 同步 setKpiMeasuredBy 同事务化 + Milvus 写入不阻塞事件循环 |
| `b70958ea` | fix: Milvus 写入锁下沉到函数内部 + 临界区收窄到 Milvus 调用 |
| `f8feea00` | test: 串行化守卫补上「一次 KPI 的删+插是一个临界区」的分组断言 |
| `c337b522` | feat: KPI 同步开关（KPI_SYNC_ENABLED/KPI_MATCH_VECTOR_ENABLED）+ CRUD 提交后 best-effort 钩子 |
| `a5fa8704` | test: 补 KPI 同步开关 env 优先级 + D4 钩子失败不阻塞主流程 + Settings 默认值回归 |
| `cf6e5a0f` | feat: 存量 KPI id_mapping 幂等回填脚本（三道写库闸 + DSN 单一来源） |
| `f9a09ad3` | fix: 回填循环改在循环外取 ORM 属性快照 + 失败路径测试改真撞唯一索引 |
| `d3d63476` | feat: syncMissingGraph 补 Metric 图节点 + KpiCatalog 图对账（响应字段扩展） |
| `e3b8ef44` | fix: KPI 图对账注入可 patch 的 neo4j client + 段级失败保护 |
| `46c1a660` | feat: kpi-catalog 手动同步端点（sync-missing/单条重建）+ 诊断同步健康 |
| `26f49b37` | fix: Milvus 读失败跳过向量段（防批量重算 embedding）+ fake Milvus 增删语义 |
| `61fa753a` | fix: 回填脚本 cleanup/rebuild 覆盖 4 collection（补 metric/kpi 写回段，拆清空 metric 向量的雷） |
| `ebd63c87` | feat: KPI 匹配器向量候选兜底（双闸灰度、包含式赢、阈值丢弃、歧义澄清、distance 透传） |
| `40026b1f` | test: 歧义路由加强断言（2 候选 + code 升序，钉住不猜 top1） |
| `fbc81ffa` | feat: 每日 reconcile 计数扩展 kpi 向量/图节点（kpi_published↔milvus、kpi_total↔neo4j） |
| `ebd90052` | test: KPI 对账告警断言钉住两侧数值与方向 |
| `b94b346c` | feat: KPI 目录页 admin 同步入口（页头全量对账 + 行级重新同步）+ i18n |
| `a3af2446` | test: KPI 同步用例拆出独立文件 + 409/成功提示钉住渲染文案 |

### 文件变更（`git diff --stat b6b384e5..HEAD`，48 文件 +6805/−316；零 DDL、零迁移）

**Backend（16）**

| 文件 | 变化 |
|---|---|
| `backend/app/services/kpi_vector_graph_sync.py` | 新增，同步核心（向量 PUBLISHED / 图全状态 / MEASURED_BY 边集合替换 / best-effort） |
| `backend/app/services/kpi_metric_switches.py` | 新增，两个开关读取（`isKpiSyncEnabled` / `isKpiMatchVectorEnabled`） |
| `backend/app/services/kpi_catalog_service.py` | CRUD 提交后 best-effort 钩子 |
| `backend/app/services/kpi_semantic_match_service.py` | 向量候选步（`matchRouteVector`） |
| `backend/app/services/ontology_service.py` | `syncMissingGraph` 覆盖 Metric/KpiCatalog + 计数 |
| `backend/app/services/knowledge_compiler_service.py` | 每日对账计数扩展 `kpi_published`/`kpi_total` |
| `backend/app/services/chat_stream_output.py` | distance 透传 |
| `backend/app/api/v1/kpi_catalog.py` | `POST /embeddings/sync-missing`、`POST /{id}/embedding` |
| `backend/app/api/v1/kpi_diagnose.py` | 诊断同步健康（`syncHealth`） |
| `backend/app/api/v1/ontology.py` | 对账响应字段扩展 |
| `backend/app/infrastructure/milvus_client.py` | kpi collection 可选探针 / 路由 |
| `backend/app/infrastructure/milvus_dual_write.py` | `"kpi"` type 路由 + `ensureKpiCollection` |
| `backend/app/infrastructure/milvus_query_helpers.py` | kpi 读路径 |
| `backend/app/infrastructure/milvus_search.py` | kpi 检索分支 |
| `backend/app/infrastructure/neo4j_client.py` | `KpiCatalog` 节点 / `MEASURED_BY` 边 / `getKpiCatalogIds` |
| `backend/app/config.py` | `kpiSyncEnabled`（默认 true）、`kpiMatchVectorEnabled`（默认 false） |

**Scripts（2）**

| 文件 | 变化 |
|---|---|
| `backend/scripts/backfill_kpi_id_mapping.py` | 新增，存量 KPI id_mapping 幂等回填（三道写库闸 + DSN 单一来源） |
| `backend/scripts/backfill_milvus_embeddings.py` | `_backfill` 补 metric/kpi 段；`--cleanup`/rebuild 覆盖 4 collection |

**Frontend（5）**

| 文件 | 变化 |
|---|---|
| `frontend/src/api/kpiCatalog.ts` | 两个同步端点客户端 |
| `frontend/src/types/kpiCatalog.ts` | 同步响应/健康类型 |
| `frontend/src/pages/KpiCatalogPage.tsx` | 页头全量对账 + 行级重新同步 |
| `frontend/src/i18n/{zh-CN,en-US}.ts` | 同步入口文案（中英齐全） |

**Docs（1）**

| 文件 | 变化 |
|---|---|
| `docs/superpowers/plans/2026-11-13-kpi-vector-graph-sync.md` | 计划围栏修正（commit `9104500b`） |

**Tests（24）**

| 段 | 文件 |
|---|---|
| 新增单测（9） | `test_backfill_milvus_script.py`、`test_kpi_catalog_hooks.py`、`test_kpi_match_vector.py`、`test_kpi_sync_switches.py`、`test_kpi_vector_graph_sync.py`、`test_milvus_kpi_routing.py`、`test_milvus_optional_collection.py`、`test_neo4j_kpi_catalog.py`、`test_reconcile_kpi_counts.py` |
| 新增集成（5） | `test_backfill_expected_from_pg.py`、`test_backfill_kpi_id_mapping.py`、`test_kpi_graph_sync_integration.py`、`test_kpi_reconcile_graph.py`、`test_kpi_sync_endpoints.py` |
| 修改（7） | `test_knowledge_compiler_reconcile.py`、`test_kpi_diagnose_api.py`、`test_kpi_match_corpus.py`、`test_ontology_reconcile_sync.py`、`test_milvus_collection_rebuild.py`、`test_milvus_search_connection.py`、`conftest.py`（测试环境 `KPI_SYNC_ENABLED=false` 隔离） |
| 新增前端（1） | `frontend/src/tests/KpiCatalogSync.test.tsx` |
| 修改前端（2） | `frontend/src/tests/KpiCatalogPage.test.tsx`、`frontend/src/tests/kpiCatalogI18n.test.ts`（扩展） |

> 说明：conftest 的 `KPI_SYNC_ENABLED=false` 只是**测试环境显式覆盖**，不改变代码默认值（默认 on）。
> 原因：best-effort 钩子会自开 session 并用 app 侧配置连 Neo4j（默认 7687 = 生产），半隔离比不隔离更危险。

## 验收证据（spec §11 六条）

**回归口径**：后端 39 个测试文件（新建 14 / 修改 6 / 邻近未动 19），前端 4 个文件；
统一 `./.venv/bin/python -m pytest`、`TEST_DATABASE_URL=…:5434/qa_metadata_test`、
`TEST_NEO4J_URI=bolt://127.0.0.1:7688`；未连接 Milvus 19530 / Neo4j 7687。

| # | 验收条 | 测试文件 + 用例 | 实测结论 |
|---|---|---|---|
| 1 | 新建 KPI 自动建向量/图；diagnose 可见 | `unit/test_kpi_catalog_hooks.py::test_create_published_syncs_graph_and_vector`、`test_create_draft_syncs_graph_only`；`integration/test_kpi_diagnose_api.py::test_diagnose_reports_sync_health` | create 提交后同时调用 graphSynced + vectorSynced（DRAFT 只建图）；diagnose 返回 `syncHealth` 三计数（PG 1 / 向量 3 / 图 2）且 `errors=[]`——PASS |
| 2 | 转 DRAFT 删向量、留图节点 | `unit/test_kpi_catalog_hooks.py::test_update_unpublish_deletes_vector_keeps_graph`；`integration/test_kpi_graph_sync_integration.py::test_vector_sync_published_then_draft`；`unit/test_milvus_kpi_routing.py::test_search_none_sweep_covers_4_collections` | PUBLISHED→DRAFT ⇒ `vectorDeleted=[id]`、`graphSynced=[id]`（节点保留、status 更新）；4 collection 扫描路径覆盖 kpi——PASS |
| 3 | `sync-missing` 幂等 + 修复缺失 | `integration/test_kpi_sync_endpoints.py::test_sync_missing_endpoint_idempotent`、`test_sync_missing_endpoint_syncs_absent_vector`、`test_sync_missing_endpoint_deletes_orphan_vectors`、`test_sync_missing_second_round_after_sync_is_noop`；`integration/test_kpi_reconcile_graph.py::test_sync_missing_graph_covers_metric_and_kpi`；`integration/test_kpi_graph_sync_integration.py::test_graph_sync_registers_mapping_and_upserts` | kpi 向量：首轮补齐缺失/删孤儿后，复跑 `vectorSyncedCount/vectorOrphanCount/graphSyncedCount/failedCount` 全 0。Metric 图节点：`syncMissingGraph` 在库内仅 1 metric + 1 KPI 时返回 `totalMetrics=1 / syncedMetricCount=1 / missingMetricCount=1`，`metricUpsert` 收到 `obj:METRIC:{id}`（name/alias/formula/aggFunction 全字段），且 KPI 段随后建出 `MEASURED_BY` 边 ⇒ 指标图节点缺失可修复。KPI 图：`syncKpiGraphNow` 注册 id_mapping 并 upsert 节点——PASS |
| 4 | 回填 `--cleanup` 后 4 collection 恢复 | `unit/test_backfill_milvus_script.py::test_cleanup_writes_back_all_four_types`、`test_cleanup_refills_missing_metric_and_kpi_rows`、`test_read_all_rows_covers_four_collections`、`test_write_back_records_preserve_external_id` | fake Milvus 下 cleanup 读/写回覆盖 class/property/metric/kpi 四类，metric+kpi 缺失行被补回，`external_id` 保留——PASS。**注意：仅 fake 夹具，未对真实 Milvus 执行 `--cleanup`。** |
| 5 | 灰度：`match_method=vector` / 歧义澄清 / 关闸回退 | `unit/test_kpi_match_vector.py::test_single_vector_hit`、`test_metric_hit_reverse_binds_kpi`、`test_two_hits_ambiguous`、`test_containment_wins_vector_not_called`、`test_over_threshold_dropped`、`test_milvus_failure_silent_fallback`；`unit/test_kpi_sync_switches.py::test_settings_defaults`、`test_match_vector_default_off_when_row_missing`、`test_match_vector_env_kill_wins_over_true_row`、`test_sync_enabled_default_on_when_row_missing` | 单命中 `method="vector"` + distance 透传；metric 命中经 `metric_id` 反查绑定 KPI；2 候选 ⇒ ambiguous（code 升序，不猜 top1）；包含式命中优先且不发起向量检索；超阈值丢弃；Milvus 异常静默回落；双闸默认 off/on 与 env 杀闸均符合预期——PASS |
| 6 | 净新增红为 0 | 见下方失败集合对差 | **14 个新建文件 146 passed / 0 failed**；6 个修改文件 39 用例中 11 failed，与 base `b6b384e5` 的失败集合**逐条同名**（base 同样 11 failed）；19 个邻近文件 284 passed / 0 failed ⇒ **净新增红 0** |

### 失败集合对差取证（#6）

当前分支（`a3af2446`）：

```
app/tests/unit/test_*.py (新建 9) + app/tests/integration/test_*.py (新建 5)
  ⇒ 146 passed, 0 failed（52.90s）

修改的 6 个测试文件（39 用例）
  ⇒ 28 passed, 11 failed

邻近未改动的 19 个测试文件
  ⇒ 284 passed, 0 failed（88.21s）
```

11 条红（全部为既有基线红，非本批引入）：

```
test_knowledge_compiler_reconcile.py::test_reconcile_real_pg_empty_diff_zero_writes_audit
test_knowledge_compiler_reconcile.py::test_reconcile_detects_mismatch_against_real_pg
test_knowledge_compiler_reconcile.py::test_reconcile_is_read_only_on_pg
test_ontology_reconcile_sync.py::TestEmbeddingSyncCoversProperties::test_syncs_missing_class_and_property_vectors
test_ontology_reconcile_sync.py::TestEmbeddingSyncCoversProperties::test_present_vectors_skipped
test_ontology_reconcile_sync.py::TestEmbeddingSyncCoversProperties::test_property_embedding_failure_isolated
test_ontology_reconcile_sync.py::TestGraphSyncMissing::test_backfills_missing_graph_entities
test_ontology_reconcile_sync.py::TestGraphSyncMissing::test_skips_existing_graph_entities
test_ontology_reconcile_sync.py::TestGraphSyncMissing::test_relation_link_failure_isolated
test_ontology_reconcile_sync.py::TestReconcileEndpoints::test_api_sync_missing_embeddings_returns_property_fields
test_ontology_reconcile_sync.py::TestReconcileEndpoints::test_api_graph_sync_missing
```

**base 对照（`b6b384e5`）**：临时 worktree `/tmp/qa-kpi-base`（用后已 remove），因本仓库是 editable
安装、`.pth`/editable finder 指向**当前** worktree，故在该 worktree 内用 `uv sync --frozen --extra dev`
建了独立 venv，并先自证导入路径：

```
$ cd /tmp/qa-kpi-base/backend && ./.venv/bin/python -c "import app; print(app.__file__)"
/private/tmp/qa-kpi-base/backend/app/__init__.py
```

（对照证明：用本 worktree 的 `.venv/bin/python` 在 `/tmp` 下 import 时打印的是
`…/.worktrees/feat-kpi-vector-graph-sync/backend/app/__init__.py` ⇒ editable 确实指向当前分支。）

在 base 上跑上述两个文件：

```
11 failed, 4 passed（失败用例名与本分支逐条同名、同断言）
```

根因（Task 10 已裁定）：autouse `warmAgentCaches` → `seedHeaderOntologyClasses` 每个测试
seed 5 个 header 类，与 `class==0` / `counts==2` 断言冲突（观察值 7 vs 2）。本批**未修改**这些用例的断言。

> 注：其余 4 个修改文件（`test_kpi_diagnose_api.py` / `test_kpi_match_corpus.py` /
> `test_milvus_collection_rebuild.py` / `test_milvus_search_connection.py`）全部 PASS，无需 base 取证。

### 前端回归

```
npx tsc --noEmit                                  ⇒ 0 错误
npx vitest run src/tests/KpiCatalogPage.test.tsx \
               src/tests/KpiCatalogSync.test.tsx \
               src/tests/kpiCatalogI18n.test.ts \
               src/i18n/i18n.test.ts              ⇒ 4 files / 69 tests passed
```

## 开关

| 开关 | env 默认 | `system_config` 行缺失 | 语义 |
|---|---|---|---|
| `KPI_SYNC_ENABLED` | **on**（`config.py` default=True） | **on**（`readKpiMetricSwitch(..., default=True)`） | 向量/图同步钩子。**方向与既有开关相反**：同步是存储面能力，缺行回落 on 才是「无行为变化」；关同步须显式把行置 false。env 只作进程级一键止血。 |
| `KPI_MATCH_VECTOR_ENABLED` | **off**（default=False） | **off**（fail-closed） | 匹配器向量候选步。向量召回改变 chat 行为 ⇒ 缺行/非法/读取异常一律关；开启是显式灰度操作。 |

⚠️ **`KPI_MATCH_VECTOR_ENABLED` 是双闸**：env 必须为 true **且** `system_config` 行为 true 才生效
（`isKpiMatchVectorEnabled`：先查 env 杀闸，再查行）——单独配行**不会**启用。
`KPI_SYNC_ENABLED` 同样是「env 杀闸 + 行灰度」两段，但方向相反（缺行 on）。

## 未做的事

- **不部署生产**：生产发版硬闸门为「迁移→回填→再上代码」，另行决策；本批零 DDL，但仍未部署。
- 生产库未动；`id_mapping` 存量回填在 dev 5433 实跑**待用户批准**。
- 不引入 outbox / worker 可靠队列（D4 裁决）。
- NL2SQL schema 裁剪**不**消费 metric 向量（消费面只加匹配器向量候选）。
- 子项目② P5 治理收口、子项目③ P4b 编译器不在本批。
- 真实 Milvus 上的 `--cleanup` 未执行（仅有 fake 夹具测试）。

## 部署后人工冒烟清单（本批不执行，只列）

1. 新建 PUBLISHED KPI → 向量/图节点出现 → diagnose `syncHealth` 可见。
2. 转 DRAFT → 向量消失、图节点保留（status 更新）。
3. `POST /api/v1/kpi-catalog/embeddings/sync-missing` 幂等复跑 → counts 全 0。
4. 开灰度（env + `system_config` 行双 true）→ 语义相近问句命中 `match_method=vector`；歧义走澄清。
5. 回填脚本 `--cleanup` 后 4 个 collection 全部恢复（class/property/metric/kpi）。
