# 变更：KPI 业务目录种子（Phase 4.2）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 4.2（feat-kpi-catalog-governance 的种子阶段）
- **状态**：done
- **前置**：feat-kpi-catalog-governance（KpiCatalog 表 + CRUD API）

## 1. 需求

为采购域（采购域.md §六 Sheet 13）填充 12 个核心 KPI 种子，覆盖 4 类：

| 类别 | 数量 | KPI |
|---|---|---|
| 交付类 | 4 | KPI_SUPPLIER_OTD / KPI_SUPPLIER_OVERDUE_RATIO / KPI_PURCHASE_CYCLE_TIME / KPI_SUPPLIER_DELAY_DAYS |
| 质量类 | 3 | KPI_SUPPLIER_DEFECT_RATE / KPI_SUPPLIER_FPY / KPI_SUPPLIER_NCR_RATE |
| 价格类 | 2 | KPI_PURCHASE_PRICE_VARIANCE / KPI_COST_SAVING |
| 财务/合规 | 3 | KPI_INVOICE_MATCH_RATE / KPI_PAYMENT_ON_TIME / KPI_SUPPLIER_MAINTENANCE_RATIO |

为 Phase 5 Supplier 360° 视图、Phase 6 Supplier Risk Agent 提供治理层元数据；为 Chat / AI 应用提供「数据可信度」指标清单。

## 2. 设计评审

- **数据语义对齐采购域 §六 Sheet 13**：分子/分母/粒度/单位/数据来源/Owner 全部按 doc 写入
- **status 全部 PUBLISHED**：种子阶段代表「治理委员会已认可的口径」，不是 DRAFT
- **metric_id 全部 NULL**：业务治理 KPI 与技术 ontology_metric 是弱关联；本批次不绑定，留给 Phase 4.3+ Feature Layer 显式 link
- **不写 Neo4j / Milvus**：KPI 治理层不入向量检索（与 Phase 4.1 决策一致）
- **幂等性**：通过 `INSERT ... ON CONFLICT (kpi_code) DO NOTHING` 实现（PG 专属语法）；唯一索引 `uq_kpi_catalog_code` 冲突时 DB 静默跳过

## 3. 数据模型

复用 `kpi_catalog` 表（Alembic `0023_kpi_catalog`，已在 Phase 4.1 落地）：

每行 14 个字段，与 Phase 4.1 ORM 一致：
- 业务字段：kpi_code / kpi_name / business_definition / formula / numerator / denominator / grain / unit / data_source
- 治理字段：owner / version / revision_count / status / metric_id
- 审计字段：created_by / created_time / updated_time

## 4. 接口契约

无新增 API。Phase 4.1 已提供的 5 端点（list/get/create/update/delete @ `/api/v1/kpi-catalog`）即可消费种子数据。

## 5. 实现要点

- `backend/scripts/seed_kpi_catalog.py`（248 行）：
  - `KPI_SEEDS: list[dict]` — 12 条种子定义（按 4 类分组注释）
  - `seedKpiCatalog(session)` — async 幂等写入，返回本次新增条数
  - `main()` — 独立运行入口（`python scripts/seed_kpi_catalog.py`）
- `backend/app/tests/integration/test_seed_kpi_catalog.py`（110 行，5 用例）：
  - 12 条插入
  - 幂等性（第二次 0 新增）
  - 字段完整性（每个 kpi_code 至少有 kpi_name/status/owner/unit/grain/version）
  - 4 类覆盖（交付/质量/价格/财务）
  - 采购域 §六 重点 KPI（OTD + Price Variance）必含且 PUBLISHED

## 6. 测试

**集成测试（5 用例，全部 PASS，真实 PG 5433）**：

`backend/app/tests/integration/test_seed_kpi_catalog.py`：
- `test_seed_inserts_twelve_kpis` 首次运行 12 条
- `test_seed_is_idempotent` 第二次运行 0 新增，总数 12
- `test_all_seeds_have_required_fields` 12 行字段完整性
- `test_sheet13_categories_covered` 4 类全覆盖（12 个 kpi_code 全部命中）
- `test_procurement_doc_required_kpis_present` 采购域 §六 重点示例（OTD + Price Variance）字段一致

**覆盖率**：
- 全量后端：**1388 passed**（1374 baseline + Phase 4.1 新增 26 + Phase 4.2 新增 5 + Phase schema drift 9 = 1388 + ...；逐 phase 累加，最终态）

## 7. 审查

无新引入的 service / API / ORM，纯种子数据；数据来源严格遵循 `docs/data-knowledge/采购域.md §六 Sheet 13`。code-reviewer / security-reviewer 评估维度：脚本逻辑（幂等/字段完整/可独立运行）+ 测试覆盖。

## 8. 部署与验证

**运行种子**：

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  .venv/bin/python scripts/seed_kpi_catalog.py

# 期望：[seed_kpi_catalog] 本次新增 12 条，库内共 12 条
# 期望：✅ KPI 业务目录种子完成（12 条核心 KPI：4 交付 + 3 质量 + 2 价格 + 3 财务/合规，Phase 4.2 验收 ≥12 条已满足）
```

**API 冒烟**：

```bash
curl http://localhost:8000/api/v1/kpi-catalog | jq '.[].kpiCode'
# 期望：12 个 KPI_SUPPLIER_OTD / KPI_PURCHASE_PRICE_VARIANCE / ...
```

**前端**：`/kpi-catalog` 页面打开即看到 12 条数据，按 kpi_code 升序排列，状态 Tag 全部为绿色（PUBLISHED）。

## 9. 真实数据验证（Harness 门禁）

- 真实 PostgreSQL 5433：seed 插入成功（12 条）
- 真实 PostgreSQL 5433：第二次运行幂等（0 新增）
- 真实 PostgreSQL 5433：5 个集成测试全部通过，字段语义对齐采购域 doc
- 数据语义对齐：每个 KPI 的分子/分母/数据来源/Grain/Unit 都与采购域 §六 Sheet 13 描述一致

## 10. 决策与遗留

**已落地的决策**：
1. status 全部 PUBLISHED — 种子阶段视为「治理委员会已认可的口径」
2. metric_id 全部 NULL — 与 ontology_metric 弱关联留给 Phase 4.3+ Feature Layer 显式 link
3. created_by 全部 `seed_phase4_2` — 标识来源，便于追溯
4. 幂等性用 `INSERT ... ON CONFLICT DO NOTHING`（PG 专属）— 并发安全，无 TOCTOU

**遗留（下次迭代）**：
- **Owner-based ACL / 审计 / 历史快照**（feat-kpi-catalog-governance Phase 4.1 §10 遗留）— task #68
- KPI ↔ ontology_metric 显式 link：当前 metric_id 全部 NULL；下一阶段按指标家族绑定（如 OTD → 不需绑定、Price Variance → KPI_AVG_PRICE）
- **Phase 4.3 feat-ai-feature-model**：FeatureDefinition + FeatureValue 双表 + 计算作业（基于 KPI 种子定义）

**相关 Change**：
- 前置：`feat-kpi-catalog-governance`（KpiCatalog 表 + CRUD API + frontend 页面）
- 后继：Phase 4.3 AI Feature Layer；Phase 5 Supplier 360° 视图消费 KPI 治理字段