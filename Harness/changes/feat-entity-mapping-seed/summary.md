# 变更：entity_mapping 跨系统编码映射种子（Phase 3.2）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 3（L3 数据治理 — 跨系统编码映射）
- **状态**：done

## 1. 需求

为 Phase 3.1 的 `entity_mapping` 表灌入跨系统编码映射种子，使「供应商 100001 在 ERP/SRM/QMS 的编码」类跨系统追溯问题有真实数据可回答（满足 AI-Ready 标准体系「命名与编码」「主数据与一致性」、采购域 Sheet 04 主数据 + Sheet 05 映射示例语义）。

**验收标准**：
- 种子脚本幂等：重复运行不产生重复数据（唯一键查重 + DB 约束兜底）
- 45 条映射：25 供应商 + 15 物料 + 3 PO + 2 GR/IQC
- 覆盖 ERP / SRM / QMS 三源系统（跨系统追溯验收）
- 匹配规则两态：SUPPLIER/MATERIAL 用 MDM_MASTER；PO/GR/IQC 用 BUSINESS_KEY（业务键语义）
- 可通过 HTTP API 列表查询到种子数据

## 2. 设计评审

**已确认的关键决策**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 幂等策略 | PG `INSERT ... ON CONFLICT DO NOTHING`（index_elements = entity_type+enterprise_key+source_system） | 无 TOCTOU race、并发安全、冲突行由 DB 直接跳过；评审收敛后替代「预查重+插入」（后者并发下有竞态） |
| 脚本可测性 | 核心逻辑抽成 `seedEntityMappings(session) -> int`，`main()` 只做 CLI 入口 | 使幂等性/数据规模可被集成测试直接断言（TDD），而非只靠人工跑脚本验证 |
| `scripts/` 包化 | 新增 `backend/scripts/__init__.py` | 让集成测试能 `from scripts.seed_entity_mapping import seedEntityMappings` |
| 种子数据分布 | 25 供应商（10×ERP/SRM + 5×QMS）、15 物料（10×ERP + 5×SRM）、3 PO、2 GR/IQC | 对齐计划文档数量；部分实体未全系统注册（QMS/SRM 只覆盖前 5）更贴近真实主数据形态 |
| 匹配规则映射 | SUPPLIER/MATERIAL → MDM_MASTER；PO/GR/IQC → BUSINESS_KEY | 供应商/物料以 MDM 主数据为准；单据类以业务键（单据号）匹配，符合 Sheet 04「Business Key vs 代理键」区分 |
| source_code 语义 | 种子中取源系统 key（source_code = source_key） | Sheet 05 语义：源侧标识即源编码；与 Phase 3.1 测试基线一致 |
| 有效期 | effective_date=2026-01-01，expiry_date=NULL | 长期有效映射，NULL 语义已在 Phase 3.1 定义 |

**多视角审视**：
- **后端视角**：种子写入走 ORM（`EntityMapping(**m)`），无 raw SQL；查重走 `select()` ORM 条件
- **数据视角**：企业侧标识（key + code）与源侧标识（system + key/code）分层清晰；PO 号等业务键同时作为 enterprise_code 与 source_key
- **不可变性视角**：`_mapping()` 返回新 dict，不修改入参；构建数据用函数返回新 list

## 3. 数据模型变更

无表结构变更。复用 Phase 3.1 的 `entity_mapping` 表（`0021_entity_mapping`）。

## 4. 接口契约变更

无 API 契约变更。种子数据通过既有 `GET /api/v1/entity-mappings` 可查。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/scripts/seed_entity_mapping.py` | 新文件：45 条种子数据（4 个构建函数）+ `seedEntityMappings(session)` 幂等写入 + `main()` CLI 入口 |
| `backend/scripts/__init__.py` | 新增：scripts 包化，支持集成测试导入 |
| `backend/app/tests/integration/test_seed_entity_mapping.py` | 新文件：9 条集成测试（真实 PG 5433） |

**种子数据构成**：
- `_supplierMappings()`：10 家供应商 × {ERP(V 前缀), SRM(S 前缀)} + 前 5 家 × QMS(Q 前缀) = **25**
- `_materialMappings()`：10 个物料（RM-STEEL-001..010）× ERP（源侧即物料编码）+ 前 5 个 × SRM(MS 前缀) = **15**
- `_poMappings()`：3 条 PO（PO202608001-003，业务键 = 单据号，BUSINESS_KEY）= **3**
- `_grIqcMappings()`：1 GR（GR202608001/ERP）+ 1 IQC（IQC202608001/QMS）= **2**

**关键不可变性细节**：
- `_mapping()` 返回全新 dict，构造行不修改调用方状态
- 数据构建函数返回新 list，模块级 `ALL_MAPPINGS` 为四段拼接的只读常量
- `seedEntityMappings` 逐条 `pg_insert(...).on_conflict_do_nothing(index_elements=[...])`：唯一索引 `uq_entity_mapping_entity_source`（entity_type+enterprise_key+source_system）冲突行由 DB 静默跳过，`result.rowcount` 累加得本次新增数，单次 commit

## 6. 测试

**后端集成测试**（真实 PG 5433，`backend/app/tests/integration/test_seed_entity_mapping.py`，9 测试）：
- `test_first_run_inserts_all_mappings`：首次运行新增 45 条 + 显式断言库内共 45 条
- `test_second_run_is_idempotent`：二次运行新增 0 条 + 显式断言总数仍 45（不只依赖返回值）
- `test_partial_pre_existing_state_inserts_only_missing`：预置 1 条重叠映射 → seed 只补 44 条，总数保持 45
- `test_row_counts_by_entity_type`：按实体类型分布（25/15/3/1/1）
- `test_covers_erp_srm_qms_systems`：覆盖 ERP/SRM/QMS 三系统
- `test_dates_and_source_values_contract`：有效期契约（effective=2026-01-01、expiry=NULL）+ ERP 供应商源键 V 前缀形态抽查
- `test_po_gr_iqc_use_business_key_rule`：单据类全部 BUSINESS_KEY
- `test_supplier_material_use_mdm_master_rule`：主数据类全部 MDM_MASTER
- `test_api_list_returns_seeded_rows`：HTTP 链路可查到 45 条 + 抽查 SUP 前缀

**测试结果**：
- 本测试文件：9/9 PASS
- 后端全量：**1315 passed**，覆盖率 **93.21%**（≥80% 门禁）
- CLI 幂等实测：首次「新增 45 条，库内共 45 条」，重复「新增 0 条，库内共 45 条」

## 7. 安全审查

**触发场景**：无 HTTP 入口、无用户输入（数据为脚本内常量）、仅 ORM 写入。安全面极小，由 code-reviewer / security-reviewer 双 agent 并行审查。

**审查结果**：见 §9.3。

## 8. 部署验证

```bash
cd backend

# standalone 运行（幂等，可重复执行）
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run python scripts/seed_entity_mapping.py
# → [seed_entity_mapping] 本次新增 45 条，库内共 45 条（首次）
# → [seed_entity_mapping] 本次新增 0 条，库内共 45 条（重复）

# 集成测试
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/integration/test_seed_entity_mapping.py -v
# → 9/9 PASS

# 全量回归 + 覆盖率
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80 -q
# → 1315 passed, TOTAL 93.21%
```

## 9. 真实数据验证（Harness 门禁）

集成测试全程走真实 PG 5433 + 真实 ORM + 完整 API 链路；`scripts/` 种子核心函数被测试直接调用，CLI 入口单独实测幂等。

### 9.1 验证结果（2026-08-30）

```
test_first_run_inserts_all_mappings PASSED
test_second_run_is_idempotent PASSED
test_partial_pre_existing_state_inserts_only_missing PASSED
test_row_counts_by_entity_type PASSED
test_covers_erp_srm_qms_systems PASSED
test_dates_and_source_values_contract PASSED
test_po_gr_iqc_use_business_key_rule PASSED
test_supplier_material_use_mdm_master_rule PASSED
test_api_list_returns_seeded_rows PASSED
========================= 9 passed =========================
```

| 检查项 | 期望 | 实测 | 结论 |
|---|---|---|---|
| 首次运行插入 | 45 条 | 45 | ✅ |
| 二次运行幂等 | 0 条新增 + 总数仍 45 | 0 / 45 | ✅ |
| 预置重叠状态 | 只补缺失 44 条 | 44 / 45 | ✅ |
| 实体类型分布 | 25/15/3/1/1 | 25/15/3/1/1 | ✅ |
| 源系统覆盖 | ERP/SRM/QMS | 全覆盖 | ✅ |
| 有效期契约 | effective=2026-01-01 / expiry=NULL | 全符合 | ✅ |
| 单据类匹配规则 | 5 条全 BUSINESS_KEY | 全 BUSINESS_KEY | ✅ |
| 主数据类匹配规则 | 40 条全 MDM_MASTER | 全 MDM_MASTER | ✅ |
| HTTP 列表 | 45 条可查 | 45 | ✅ |
| CLI 幂等（两次） | 45 新增 → 0 新增 | 45 / 0 | ✅ |

### 9.2 数据契约 Roundtrip 一致性

种子 `EntityMapping(**m)` 与 Phase 3.1 `EntityMappingCreate` 字段一一对应（snake_case ORM ↔ camelCase JSON），经 `entityMappingToRead` 输出与前端 `EntityMappingRead` 类型对齐。种子写入不绕过 service 层校验（数据为脚本内常量，已保证合法性）。

### 9.3 代码审查结果

**双 agent 并行审查（code-reviewer + security-reviewer）**：两者均 **APPROVED**，无 CRITICAL / HIGH 阻断项。

| Agent | 结论 | 发现 |
|---|---|---|
| code-reviewer | APPROVED | 1 MEDIUM + 3 LOW |
| security-reviewer | APPROVED | 6 LOW（信息级，无注入/凭据/越权风险；脚本无 HTTP 入口、无用户输入） |

**评审收敛点与修复**：

| # | 发现 | 级别 | 修复 |
|---|---|---|---|
| 1 | 并发 commit 时 IntegrityError 未捕获（预查重+插入存在 TOCTOU race） | MEDIUM | 改为 PG `INSERT ... ON CONFLICT DO NOTHING`（`index_elements`），冲突行由 DB 跳过，无 race、并发安全 |
| 2 | 二次运行测试只断言返回值 0，未断言库内总数 | LOW | `test_second_run_is_idempotent` 增加 `count==45` 显式断言 |
| 3 | 未覆盖「部分数据已预置」场景 | LOW | 新增 `test_partial_pre_existing_state_inserts_only_missing`（预置 1 条 → seed 补 44，总数 45） |
| 4 | 未断言日期/source 值契约 | LOW | 新增 `test_dates_and_source_values_contract`（effective/expiry/source_key 形态抽查） |
| 5 | main() 全表计数与 seed 共用一次会话更稳妥 | LOW | main() 改为同会话 `select(func.count())` 统计总数 |
| 6 | 约束名写死 uq_entity_mapping_entity_source，但迁移建的是**唯一索引**而非命名的表约束 | LOW | 改用 `index_elements=["entity_type","enterprise_key","source_system"]` 指向唯一索引列，规避 `ON CONSTRAINT` 对索引不生效的 PG 语义 |

**修复后验证**：9/9 集成测试 PASS；全量 1315 passed，覆盖率 93.21%；CLI 两次运行「45 → 0」幂等成立。

## 10. 关联

- 计划：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` Phase 3.2
- 前置：`Harness/changes/feat-entity-mapping-model/summary.md`（Phase 3.1，entity_mapping 表 + CRUD）
- 模板：`Harness/changes/feat-data-lineage-model/summary.md` + `feat-entity-mapping-model/summary.md`
- 下一阶段：`feat-missing-business-objects`（Phase 3.3，RFQ/QUOT/CONTRACT/ASN/NCR/INV/PAY/SUP_PERF 8 个对象建模）
- 规则：`Harness/rules/开发流程规范.md`（10 阶段工作流）
