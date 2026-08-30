# 变更：采购域缺失业务对象建模（Phase 3.3）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 3（L3 数据治理 — 缺失对象建模）
- **状态**：done

## 1. 需求

把采购域缺失的核心业务对象建模完整（计划文档 Change 3.3 原列 8 个对象），使 NL2SQL 能引用「报价/发票/付款」类单据回答采购域问题（满足 AI-Ready 标准体系采购域 Sheet 03 业务对象目录语义）。

**范围修正（用户已确认 + 实证收敛）**：原 8 个对象中仅建模**有真实源表的 4 个对象 / 6 个类**，其余 4 个以「已知缺口」记录而非强行造表：

| 对象 | 处理 | 说明 |
|---|---|---|
| QUOT（报价） | ✅ 建模 | 映射 Sage X3 **采购报价 `PQUOTAT`/`PQUOTATD`**（修正：`SQUOTE` 是销售报价，不属采购域） |
| INV（采购发票） | ✅ 建模 | `PINVOICE`/`PINVOICED`（采购发票及明细） |
| PAY（付款） | ✅ 建模 | `PAYMENTH`/`PAYMENTD`（付款单及明细） |
| RFQ（询价） | ✅ 并入 QUOT | `PQUOTAT` 一张单据覆盖「询价 → 报价」两端（`PSHNUM_0` 引用来源请购） |
| ASN（发运通知） | ✅ 已覆盖 | = 到货单 `ArrivalNotice`（`YPRECEIPT`），Phase 2 已建模 |
| DELIVERY（交付） | ✅ 已覆盖 | 采购交付由 `YPRECEIPT`/`PRECEIPT` 全流程覆盖（`SDELIVERY` 为销售发货） |
| CONTRACT（采购合同） | ⚠️ 已知缺口 | ZJTH 无合同主数据表；留待 Phase 5 DocumentCatalog |
| NCR（不合格处理） | ⚠️ 已知缺口 | 属 QMS 域，当前业务库无 NCR 表 |
| SUP_PERF（供应商绩效） | ⚠️ 已知缺口 | 派生指标域，由 Phase 4 Feature/KPI 计算，无源表 |

**验收标准**：
- 6 个新类（3 对象 × 头/明细）经 `seed_ontology.py` 注册，source_table 指向真实 ZJTH 表
- 表头单主键、明细复合主键（单据号+行号）且 FK 指回表头；关键 FK（物料→ITMMASTER、供应商→BPARTNER）齐备
- 4 条复合键业务流转边（报价→请购、发票→订单/收货/付款三向匹配）
- 3 个新 KPI（发票金额 / 付款金额 / 询价数量）
- seed 幂等（重跑不增行）；Milvus 与 PG 对齐；NL2SQL 能引用新类

## 2. 设计评审

**已确认的关键决策**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 建模范围 | 只建模有真实源表的 3 对象（6 类），其余 4 个记录为已知缺口 | 计划文档原列 8 对象，其中 CONTRACT/NCR/SUP_PERF 无源表、DELIVERY 已由 YPRECEIPT 覆盖；强行建模会造虚拟表，违背「真实数据」原则 |
| QUOT 映射 | Sage X3 **采购报价 `PQUOTAT`**，非销售报价 `SQUOTE` | `SQUOTE` 含 `REP_0`（销售员）/`SOHNUM_0`（销售订单）→ 销售侧；`PQUOTAT` 含 `BPSNBR_0`/`RSPNBR_0`/`PSHNUM_0`（请购引用）→ 采购侧。`PQUOTAT` 单表覆盖询价+报价两端 |
| 明细复合主键 | 表头单据号（FK 指回表头）+ 行号 双主键 | 与既有 `PORDERQ`/`PRECEIPTD` 明细模式一致（Sage X3 单据明细天然 (doc, line) 复合键） |
| 复合键 join | 4 条业务流转边走 `BUSINESS_JOINS`（非 FK 自动物化） | `_seedJoins` 仅对单 PK 目标自动物化 FK；复合键目标（PORDERQ 等）必须显式声明 |
| 金额列别名 | 列名或业务别名含「发票金额/付款金额/询价」词 | 供自然语言检索命中；属性名本身即可命中，别名补同义检索词 |
| Milvus 对齐方式 | `--cleanup` 确定性收敛（读全量→去重→补缺失→删集重建） | 增量 `--sources` 同步在重负载下产生重复行（历史已见 835 行/299 重复）；cleanup 以 PG 为唯一真源 |

**多视角审视**：
- **后端视角**：全部为静态数据结构（CLASSES/PROPERTIES/BUSINESS_JOINS/METRICS），复用既有 seed() 幂等逻辑，无新端点、无新 SQL
- **数据视角**：属性列名/别名均来自 ZJTH 真实 `all_tab_columns` 实证；发票三向匹配（PORDER/PRECEIPT/PAYMENT）与付款核销（VCRNUM_0）符合 Sage X3 四单对账模型
- **不可变性视角**：所有数据结构为模块级只读常量；无任何运行时可变全局

## 3. 数据模型变更

无表结构变更。新增内容全部写入既有 `ontology_class` / `ontology_property` / `ontology_join` / `ontology_metric` 表。

## 4. 接口契约变更

无 API 契约变更。新类经既有 `_validateOntologyAgainstSchema` 校验（`PQUOTAT`/`PINVOICE`/`PAYMENTH` 等表均存在于 ZJTH schema 缓存，无漂移告警）；`buildSchemaText` 渲染新类头与 JOIN 关系（集成测试断言）。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/seed_ontology.py` | 追加 6 个 CLASSES（Quotation/QuotationDetail/PurchaseInvoice/PurchaseInvoiceDetail/Payment/PaymentDetail）+ 6 段 PROPERTIES（含复合主键、FK、业务别名）+ 4 条 BUSINESS_JOINS + 3 个 METRICS；docstring 覆盖表 "20 类"→"27 类" |
| `backend/app/tests/unit/test_seed_missing_business_objects.py` | 新文件：10 条单元契约测试（sqlite 文件库 + monkeypatch） |
| `backend/app/tests/integration/test_seed_missing_business_objects.py` | 新文件：3 条集成测试（真实 PG 5433 + buildSchemaText） |
| `Harness/wiki/business-domain.md` | 新增「采购域业务对象目录（Phase 3.3）」章节（对象目录、QUOT 映射修正、三向匹配、KPI、已知缺口） |

**6 个新类**：

| 类 | 别名 | source_table | 主键 | 关键属性 |
|---|---|---|---|---|
| `Quotation` | 采购报价 | `PQUOTAT` | `PQHNUM_0` | 报价日期、响应期限、受邀/响应供应商数 |
| `QuotationDetail` | 采购报价明细 | `PQUOTATD` | (`PQHNUM_0`→PQUOTAT, `PQDLIN_0`) | 物料、数量、提前期、来源请购行 |
| `PurchaseInvoice` | 采购发票 | `PINVOICE` | `NUM_0` | 供应商、含税/不含税金额、到期日、状态 |
| `PurchaseInvoiceDetail` | 采购发票明细 | `PINVOICED` | (`NUM_0`→PINVOICE, `PIDLIN_0`) | 物料、数量、金额、三向匹配关联 |
| `Payment` | 付款单 | `PAYMENTH` | `NUM_0` | 付款类型、付款金额、付款/到期日期、状态 |
| `PaymentDetail` | 付款明细 | `PAYMENTD` | (`NUM_0`→PAYMENTH, `LIN_0`) | 科目、供应商、被支付凭证 |

**4 条复合键业务流转边**（`BUSINESS_JOINS`）：
- `PQUOTATD(PSHNUM_0, PSDLIN_0)` → `PREQUISD(PSHNUM_0, PSDLIN_0)`：报价明细 ← 请购明细（询价响应来源）
- `PINVOICED(POHNUM_0, POPLIN_0)` → `PORDERQ(POHNUM_0, POPLIN_0)`：发票明细 → 采购订单明细（三向匹配）
- `PINVOICED(PTHNUM_0, PTDLIN_0)` → `PRECEIPTD(PTHNUM_0, PTDLIN_0)`：发票明细 → 收货明细（三向匹配）
- `PINVOICED(PNHNUM_0, PNDLIN_0)` → `PAYMENTD(NUM_0, LIN_0)`：发票明细 → 付款明细（发票被付款核销）

**3 个新 KPI**：`KPI_INVOICE_AMT`（PINVOICED, SUM AMTNOTLIN_0）、`KPI_PAYMENT_AMT`（PAYMENTH, SUM AMTCUR_0）、`KPI_QUOTATION_QTY`（PQUOTATD, SUM QTYPUU_0）。

**seed 落库结构**：类/属性/join/指标分别写入 4 表，join 由 FK 自动物化（单 PK 目标）+ BUSINESS_JOINS（复合键目标）共同构成；`_syncToNeo4j` best-effort（新类 FK 目标均在 CLASSES 内，同步成功；仅历史遗留 FK 目标如 TABSTASTO 等警告，与本次改动无关）。

## 6. 测试

**单元测试**（`test_seed_missing_business_objects.py`，9 测试，纯契约断言无 DB）：
- `test_new_classes_registered` / `test_new_classes_have_descriptive_descriptions`：CLASSES 注册契约 + 描述含业务关键词
- `test_header_single_pk`：表头恰 1 主键（单据号）
- `test_detail_composite_pk_links_header`：明细复合主键 + 首主键 FK 指回表头
- `test_key_fks_present`：7 条关键 FK 指向正确目标类
- `test_amount_columns_have_business_aliases`：金额列名称+别名拼接含业务词
- `test_required_business_joins_present` / `test_invoice_payment_join_uses_real_columns`：4 条复合键 join 契约
- `test_new_metrics_registered`：3 个 KPI 指向正确目标表

> 幂等性不在此文件（评审收敛：禁止 sqlite 内存库 + 直接调用 seed，见 §9.3 评审表 #1），由真实 PG 集成测试 `test_seed_creates_new_classes_idempotently` 覆盖。

**集成测试**（`test_seed_missing_business_objects.py`，3 测试，真实 PG 5433）：
- `test_seed_creates_new_classes_idempotently`：6 类落库 + 重跑计数不变
- `test_new_classes_have_key_properties`：发票头/明细、付款头关键属性（主键/FK/别名）落库正确
- `test_schema_text_can_reference_new_tables`：`buildSchemaText` 渲染 3 个头类 + 明细表 + JOIN 关系区

**测试结果**：
- 本 Phase 新增：12/12 PASS
- 后端全量：**1327 passed**，覆盖率 **93.22%**（≥80% 门禁；基线 1315 / 93.21%）

## 7. 安全审查

**触发场景**：无 HTTP 入口、无用户输入（数据为脚本内常量）、无新 SQL（复用既有 seed 逻辑）。安全面极小，由 code-reviewer / security-reviewer 双 agent 并行审查。

**审查结果**：见 §9.3。

## 8. 部署验证

```bash
cd backend

# 真实 seed（幂等，可重复执行；写入 dev 元数据库 qa_metadata + Neo4j 本体图）
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  .venv/bin/python seed_ontology.py
# → 首次：6 个 class created（PQUOTAT id=181 ... PAYMENTD id=186），classes ready: 27
# → 重复：classes ready: 27（0 新创建，幂等）

# Milvus 确定性收敛（PG 为唯一真源，去重重建）
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  .venv/bin/python scripts/backfill_milvus_embeddings.py --cleanup
# → PG 期望: 27 类 + 888 属性 = 915
# → Milvus 已收敛（无重复、无缺失、无陈旧）→ 独立复核 915 = 915

# 集成测试（真实 PG 5433）
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  .venv/bin/python -m pytest app/tests/integration/test_seed_missing_business_objects.py -v
# → 3/3 PASS

# 全量回归 + 覆盖率
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  .venv/bin/python -m pytest app/tests/ --cov=app --cov-fail-under=80 -q
# → 1327 passed, TOTAL 93.22%
```

## 9. 真实数据验证（Harness 门禁）

集成测试全程走真实 PG 5433 + 真实 ORM；真实 seed 写入 dev 库 `qa_metadata`（5433），Milvus 经 `--cleanup` 确定性收敛并独立复核。

### 9.1 验证结果（2026-08-30）

**真实 seed（dev 库 qa_metadata）**：
```
首次：INFO class created ... PQUOTAT (id=181) / PQUOTATD (182) / PINVOICE (183) /
      PINVOICED (184) / PAYMENTH (185) / PAYMENTD (186) ... classes ready: 27
重复：classes ready: 27（0 新创建）
```

| 检查项 | 期望 | 实测 | 结论 |
|---|---|---|---|
| 新类落库 | 6 类 source_table 正确 | 6（PQUOTAT..PAYMENTD） | ✅ |
| 新属性 | 6 类属性齐全 | 140 条 | ✅ |
| 新 join | 4 business + FK 自动物化 | 13 条（4 business + 9 FK） | ✅ |
| 新指标 | 3 个 KPI | 3（INVOICE/PAYMENT/QUOTATION） | ✅ |
| seed 幂等 | 重跑不增行 | classes ready 仍 27，计数稳定 | ✅ |
| Milvus 对齐 | 与 PG 完全一致 | 915 = 915（0 缺失 / 0 陈旧 / 0 重复） | ✅ |
| NL2SQL 引用 | buildSchemaText 渲染新类与 JOIN | 3 头类 + 明细 + JOIN 区全渲染 | ✅ |
| 全量回归 | ≥80% 覆盖率 | 1327 passed / 93.22% | ✅ |

**Milvus 对齐独立复核**（cleanup 完成后）：`Milvus 行数 915 | 唯一实体 915`，`PG 期望 915`，`Milvus 缺 PG 有: 0`，`PG 缺 Milvus 有(陈旧): 0`。

### 9.2 数据契约 Roundtrip 一致性

新类属性名/别名/列名与 ZJTH 真实列（`all_tab_columns`）一一对应；FK 目标类（ITMMASTER/BPARTNER/FACILITY/PQUOTAT/PINVOICE/PAYMENTH）均在 CLASSES 内，`_seedJoins` 单 PK 自动物化与 `BUSINESS_JOINS` 复合键声明共同构成完整 join 图。`buildSchemaText` 集成测试断言新类头格式 `### PurchaseInvoice (采购发票): table=PINVOICE` 与 JOIN 关系区渲染。

### 9.3 代码审查结果

**双 agent 并行审查（code-reviewer + security-reviewer）**：两者均 **APPROVE / APPROVED**，无 CRITICAL / HIGH 阻断项。

| Agent | 结论 | 发现 |
|---|---|---|
| code-reviewer | APPROVE | 1 MEDIUM + 1 LOW（结构化核对全过：CLASSES/P()/复合主键/join 元数/指标键） |
| security-reviewer | APPROVED | 2 LOW（信息级，无注入/凭据/越权风险） |

**评审收敛点与修复**：

| # | 发现 | 级别 | 修复 |
|---|---|---|---|
| 1 | 单元测试 `TestMissingObjectSeed` 用 sqlite 文件库 + 直接调用 seed()，违反 `Harness/rules/测试规范.md` CRITICAL 规则（禁止 sqlite + 直接调用 service 代替真实链路），且与真实 PG 集成幂等测试重复 | MEDIUM | 删除该测试类（单元文件保留 9 个纯契约测试）；幂等由 `test_seed_creates_new_classes_idempotently`（真实 PG 5433）覆盖；docstring 注明 |
| 2 | `seed_ontology.py` docstring 表数过时：「17 张核心表」实为 27 张（26 列覆盖表 + BPCARRIER 未列入） | LOW | 行 1 改「27 张核心表」；覆盖表补 `BPCARRIER(承运商)`；行 3「缺失对象3表6类」改为「3 对象 6 表 6 类」 |
| 3 | `PAYMENTH` 属性 `信用卡号`(CRDNUM_0) 暴露在 NL2SQL 可检索本体（无数据落库，但 LLM 查询面会广告该字段存在） | LOW | 给 `CRDNUM_0` 增加 desc「（敏感字段：仅授权场景可查询/返回）」，供语义层识别；不删列（保持模型忠实度） |
| 4 | `ZJTH` 标识出现在内部 wiki/测试 docstring | LOW | 既有模式（已提交代码多处），非本次新增暴露，不处理 |

**修复后验证**：Phase 新增 12/12 PASS；全量 **1327 passed**，覆盖率 93.22%；Milvus 独立复核 915 = 915 一致。

## 10. 关联

- 计划：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` Phase 3.3
- 前置：`Harness/changes/feat-entity-mapping-seed/summary.md`（Phase 3.2）
- 模板：`Harness/changes/feat-entity-mapping-seed/summary.md`（10 段 SSOT）
- 下一阶段：`feat-ontology-governance-fields`（Phase 3.4，OntologyClass 加 object_type/object_owner 治理字段）
- 规则：`Harness/rules/开发流程规范.md`（10 阶段工作流）
