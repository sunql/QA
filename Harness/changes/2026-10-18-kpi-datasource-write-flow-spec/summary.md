# kpi_catalog.datasource_id 接入写入流 — Spec SSOT（2026-10-18）

> **状态**：🟡 SPEC-COMPLETE / IMPL-NOT-STARTED（spec-only, 不实施）
> **Spec 路径**：[`docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md`](../../docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md)
> **继承**：[F2 AIChat 生产缺陷 phase01 §1.2 (1.2a `ba3303e` + 1.2b `188cf84` + 1.2c `7b0544c`) 已 merged PR #18](https://github.com/sunql/QA/pull/18) main `aef0124e`
> **SSOT 关系**：本项**不**在 P2 SSOT `aichat-p2-followups-2026-10-16` 范围（那两条 OPEN 是 Task 12.5 minor + N4 qwen）；是 r234 recon §F2a 之后的独立后续项。
> **本 SSOT 范围**：收口 spec 落定决策 + 实施触发条件。spec 8 用例清单明确，不实施。

---

## 1. 背景

F2 三个子任务（migration 0125 + 前端 i18n + meta 帧合并）合入 PR #18 后，r234 recon §F2a 标记 "prod 5433 verified"。但 F2 的本质缺口是 "**列加了，比较加了，UI 加了，没回答列怎么填**"。全表 `datasource_id IS NULL` ⇒ chat 路径 L1 mismatch 警告（per `chat_stream_output.py:197` 的 `IS NULL ⇒ skip` 分支）实际不触发。

## 2. 本 SSOT 落地项

### 2.1 Spec 落定（2026-10-18）

4 块决策全部在 spec 显式写明：

| 决策点 | 落定值 | 替代方案 |
|---|---|---|
| DTO 必填 vs 可选 | `KpiCatalogCreate.datasource_id: int` **必填**；`KpiCatalogUpdate.datasource_id: int \| None` 可选 | 仅元信息填 (not chosen) / 完全可选 (not chosen) |
| 历史 NULL 行处置 | **保持 NULL 不动**（service docstring 标注） | batch backfill (out of scope) / 强制运营手工填 (out of scope) |
| FK 行为 | `ondelete="RESTRICT"`（migration 0126 新增） | 保留 `SET NULL` (not chosen — 误删 DS 会让 KPI 失联无迹象) |
| datasource_service.delete 行为 | **409 ConflictError** 当有 KPI 引用 | 静默 `SET NULL` (rejected) / 软删 (out of scope) |

### 2.2 8 用例清单

| 类型 | # | 描述 | 期望 |
|---|---|---|---|
| unit | U1 | `KpiCatalogCreate(datasource_id=5)` | 通过 |
| unit | U2 | `KpiCatalogCreate(datasource_id=0)` | 422 `ge=1` |
| unit | U3 | `KpiCatalogCreate(datasource_id=-1)` | 422 `ge=1` |
| integration | I1 | `createKpi` datasource 不存在 | 422 + `MSG_KPI_DATASOURCE_NOT_FOUND` |
| integration | I2 | `createKpi` datasource 失效 | 422 + `MSG_KPI_DATASOURCE_INACTIVE` |
| integration | I3 | `updateKpi` datasource_id=None 跳过校验 | 通过 + 列保持原值 |
| integration | I4 | DS 删除无 KPI 引用 | 204 + DS 消失 |
| integration | I5 | DS 删除有 1 KPI 引用 | 409 + `MSG_DATASOURCE_HAS_KPI_BINDINGS` + DS 保留 |

Per `qa-system-integration-env-contract`: **unit 也要 TEST_DATABASE_URL**；integration 5 个需 TEST_DATABASE_URL + TEST_NEO4J_URI + 真 PG 5434。

### 2.3 SSOT 入口变更

P2 SSOT `aichat-p2-followups-2026-10-16/remaining-work-ssot.md` §"真正仍 OPEN" 列表**不**增加本项（per §0 SSOT 关系注释，本项是 r234 recon F2a 后续独立 lineage，不属 P2 范围）。

r234 recon §F2a 已存在，本 spec 是其 "F2a 后续" 的下一格；不在 r234 recon 文件本身加 entry（避免给已 frozen 的 recon 添增），而**通过本 SSOT 文件建立 r234 → 本 spec 的链**。

## 3. 实施触发条件（任一满足才开新 plan）

| # | 触发 | 满足后动作 |
|---|---|---|
| 1 | 用户/产品要求"自动反查"（ontology 反查链） | 开独立 plan 走 `ontology_metric.target_class_id → ontology_class.source_table → data_source.name` |
| 2 | 用户报障 ≥ 3 次："datasource 删除失败"（per §4.3 409 行为） | 排查误用，提供 escape hatch 或文档 |
| 3 | 用户报障 ≥ 3 次："chat 警告实际不触发"（说明历史 NULL 行还没补完） | 开 backfill 工单（batch 脚本 / admin 接口 / 强制运营手工 — 由用户裁定） |

**当前状态**：3 个触发条件**均未**满足，本 spec 仅 spec-only。

## 4. 实施前必读守卫

per spec §4.5 部署前自检：

```sql
SELECT count(*) FROM kpi_catalog WHERE datasource_id IS NOT NULL;
```

期望：返回 0。若非 0，**禁止**合 migration 0126 改 FK 行为，需先触发条件 #3 的 backfill 工单。

## 5. 关联

- Spec 主文档：[`docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md`](../../docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md)
- r234 recon F2a 条目：[`Harness/changes/2026-10-15-r234-recon/summary.md` §F2a](../../Harness/changes/2026-10-15-r234-recon/summary.md)
- F2 1.2a migration：`backend/alembic/versions/0125_kpi_catalog_datasource_fk.py` (PR #18)
- F2 1.2b commit：`188cf84`（前端 i18n 查表 + 显式清除）
- F2 1.2c commit：`7b0544c`（meta 帧合并）
- PR #18（phase01 全部）：merged 2026-10-14
- P2 SSOT（**本项不属**）：[`Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md`](../../Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md)
- 主 main HEAD：`aef0124e`

## 6. 变更日志

### [2026-10-18] spec 落定（本次）

- 新增 spec：`docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md` (315 行)
- 新增本 SSOT：明确 spec-only 状态 + 4 块决策 + 8 用例清单 + 3 条触发条件
- 不实施（per 用户原始指令"不写代码, 只 spec 收口"）
- 不更新 P2 SSOT（本项不属 P2 范围）
- 不更新 r234 recon（避免给已 frozen 添增）
