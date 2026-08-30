# 变更：本体类治理字段 object_type / object_owner（Phase 3.4）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 3（L3 数据治理 — 编码映射 + 对象治理）
- **状态**：done

## 1. 需求

给 `OntologyClass` 增加 2 个治理字段（满足采购域 Sheet 03 业务对象目录要求，对应 AI-Ready 计划 Change 3.4）：

```
object_type VARCHAR(20)   # Master（主数据）/ Transaction（交易单据）/ Reference（参考/配置）/ Event（事件）
object_owner VARCHAR(100) # 责任部门/人
```

**验收标准**：
- `OntologyClass` 可创建/更新/读取这 2 个治理字段（API 全链路）
- seed_ontology 对既有 27 类回填治理字段（只增不删），重跑幂等
- 前端 ClassTab 表单（下拉选对象类型 + 输入责任部门）+ 列表列（对象类型 Tag + 责任部门）
- 覆盖率 ≥80%

## 2. 设计评审

**已确认的关键决策**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 枚举取值 | `ObjectType(str, Enum)`：Master/Transaction/Reference/Event | 与 DB `VARCHAR(20)` 兼容；与既有 `ScoreType`/`MatchRule` 枚举模式一致；Pydantic v2 自动拒绝非法值（422） |
| 27 类分布 | **12 Master / 13 Transaction / 2 Reference** | 主数据（物料/客户/合作伙伴/供应商/承运人/BOM/物料地点/地点/工艺/价格表头与明细）、交易单据（到货/收货/请购/订单/报价/发票/付款 7 组单据）、参考配置（PPRICCONF 价格配置、PREQUISO 请购订单关联） |
| 责任部门映射 | Master→`主数据管理组`，Transaction/Reference→`采购部` | 主数据归口主数据组，交易/参考单据归口采购部；Event 本期未使用不分配 |
| 迁移编号 | Alembic `0022_ontology_class_governance` | 计划文档写「0021」，但 0021 已被 Phase 3.1 `entity_mapping` 占用（与 Phase 3.1 相同的 off-by-one 平移） |
| 列可空 | 2 列均 nullable | 历史行（迁移前已有）object_type 为 NULL，由 seed 增量回填，不阻塞既有数据 |
| 回填模式 | `_seedClasses` 对既有行同步治理字段（只增不删） | 与 `_seedProperties` 别名/描述回填同模式：seed 提供才同步、未提供保留 DB 既有值，避免抹掉管理端人工维护 |
| 枚举落库 | createClass `.value`、updateClass `model_dump(exclude_unset=True)` 后归一化 | Pydantic v2 `(str, Enum)` 在 python mode 下 `model_dump()` 返回枚举成员，DB 列需字符串值 |
| 显式清空 | updateClass 传 `object_type: null` 时保留 None（不清除逻辑） | `updates.get("object_type") is not None` 跳过归一化但值仍为 None，实现「显式置空」 |

**多视角审视**：
- **后端视角**：迁移 + model + schema + service 四层同步改，均为既有 SOP；service 只在 createClass/updateClass 增加枚举归一化，无新端点、无新 SQL
- **数据视角**：object_type 分布与 Sheet 03 业务对象目录语义对齐（单据类均为 Transaction、价格配置/关联表为 Reference）；owner 归属与组织职责一致
- **前端视角**：复用 `DATA_TYPE_OPTIONS` 的 labelKey 模式（`enums.objectType.${v}`），Select 下拉 + Tag 渲染，无新依赖
- **不可变性视角**：`OBJECT_TYPE_OPTIONS` 为模块级只读常量；无运行时可变全局

## 3. 数据模型变更

`ontology_class` 表新增 2 列（Alembic 0022，两列均可空，仅加列不改既有数据）：

| 列 | 类型 | 说明 |
|---|---|---|
| `object_type` | `VARCHAR(20)` NULL | Master/Transaction/Reference/Event |
| `object_owner` | `VARCHAR(100)` NULL | 责任部门/人 |

`OntologyClass` ORM（`models.py`）同步加 `object_type`/`object_owner` 2 个 `Mapped[str | None]` 字段。

## 4. 接口契约变更

`/api/v1/ontology/classes` 增改字段（camelCase JSON 契约）：

- `OntologyClassCreate`：+ `objectType`（`ObjectType | None`，枚举校验，非法值 422）+ `objectOwner`（`str | None`，max 100）
- `OntologyClassUpdate`：同上（`exclude_unset`，仅提交字段生效）
- `OntologyClassRead`：+ `objectType`/`objectOwner`（读回一致；存量行返回 null）

向后兼容：两字段均可空，旧客户端不带字段创建/更新不受影响。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/domain/enums.py` | 新增 `ObjectType(str, Enum)`（Master/Transaction/Reference/Event） |
| `backend/app/domain/models.py` | `OntologyClass` + `object_type`/`object_owner` 2 列 |
| `backend/app/domain/schemas.py` | `OntologyClassCreate/Update/Read` + `objectType`/`objectOwner`；`ObjectType` 导入 |
| `backend/app/services/ontology_service.py` | createClass 写 `.value`；updateClass 归一化枚举成员 |
| `backend/alembic/versions/0022_ontology_class_governance.py` | 新迁移：add_column ×2 / drop_column ×2 |
| `backend/seed_ontology.py` | 27 个 CLASSES dict 各 + `object_type`/`object_owner`；`_seedClasses` 既有行回填（只增不删）；docstring 注明 |
| `backend/app/tests/unit/test_ontology_governance_fields.py` | 新文件：12 单测（枚举/DTO/seed 契约） |
| `backend/app/tests/integration/test_ontology_governance_integration.py` | 新文件：9 集成测（迁移列/API 含显式 null 清空/seed 回填幂等） |
| `frontend/src/types/ontology.ts` | `ObjectType` 联合类型 + `OBJECT_TYPE_OPTIONS`；`OntologyClass/Create/Update` + 治理字段 |
| `frontend/src/components/ontology/ClassTab.tsx` | 表单：对象类型 Select + 责任部门 Input；列表：对象类型 Tag（blue/green/orange）+ 责任部门列 |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | `enums.objectType.*` + `classColumns/classLabels/classPlaceholders` 治理字段 key |
| `frontend/src/tests/OntologyPage.test.tsx` | mockClass + 2 新测（列表渲染 Tag/责任部门；创建携带治理字段） |
| `frontend/src/tests/ontologyFilter.test.ts` | `cls()` 工厂 + 治理字段默认值 |

**27 类分布**：

| object_type | 类（source_table） |
|---|---|
| Master（12） | ItemMaster(ITMMASTER)、Customer(BPCUSTOMER)、BusinessPartner(BPARTNER)、Supplier(BPSUPPLIER)、Carrier(BPCARRIER)、BOM(BOM)、BOMDetail(BOMD)、ItemFacility(ITMFACILIT)、Facility(FACILITY)、RoutingOperation(ROUOPE)、SupplierPriceList(PPRICFICH)、SupplierPriceDetail(PPRICLIST) |
| Transaction（13） | ArrivalNotice(YPRECEIPT)、ArrivalNoticeDetail(YPRECEIPTD)、Receipt(PRECEIPT)、ReceiptDetail(PRECEIPTD)、PurchaseRequisitionDetail(PREQUISD)、PurchaseOrder(PORDER)、PurchaseOrderDetail(PORDERQ)、Quotation(PQUOTAT)、QuotationDetail(PQUOTATD)、PurchaseInvoice(PINVOICE)、PurchaseInvoiceDetail(PINVOICED)、Payment(PAYMENTH)、PaymentDetail(PAYMENTD) |
| Reference（2） | SupplierPriceConf(PPRICCONF)、RequisitionOrderLink(PREQUISO) |

## 6. 测试

**单元测试**（`test_ontology_governance_fields.py`，12 测试）：
- `TestObjectTypeEnum`：4 类取值 + 值 ≤20 字符（VARCHAR(20) 兼容）
- `TestClassSchemasGovernance`：Create 携带/可选/非法值拒绝、Update 携带、Read 序列化（`by_alias=True` 输出 camelCase + 字符串值）
- `TestSeedGovernanceMapping`：27 类治理字段齐备且合法、Master/Transaction/Reference 抽查、owner 按类型映射

**集成测试**（`test_ontology_governance_integration.py`，9 测试，真实 PG 5433）：
- `TestGovernanceMigration`：迁移后 information_schema 含 2 列
- `TestGovernanceApi`：创建携带治理字段 201、非法 objectType 422、更新生效、显式 null 清空落 NULL、列表/详情返回治理字段
- `TestGovernanceSeedBackfill`：seed 回填 27 类与 CLASSES 契约一致 + 重跑幂等（类数/字段值不变）

**测试结果**：
- 本 Phase 新增：21/21 PASS（12 单测 + 9 集成）
- 后端全量：**1348 passed**，覆盖率 **93.23%**（≥80% 门禁；基线 1327 / 93.22%）
- 前端全量：**321 passed**（+2 治理测试）；`tsc --noEmit` 通过
- Alembic head：`0022_ontology_class_governance (head)`

## 7. 安全审查

**触发场景**：API 新增 2 个用户输入字段（`objectType` 枚举 + `objectOwner` 自由文本），经 SQLAlchemy 参数化落库、React 自动转义渲染；seed 数据为脚本内常量。由 code-reviewer / security-reviewer 双 agent 并行审查。

**审查结果**：见 §9.3。

## 8. 部署验证

```bash
cd backend

# 迁移（dev 元数据库 qa_metadata）
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  .venv/bin/python -m alembic upgrade head
# → 0022_ontology_class_governance 应用，ontology_class + object_type/object_owner 2 列

# 真实 seed（回填治理字段，幂等可重复执行）
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  .venv/bin/python seed_ontology.py
# → 27 类既有行 object_type/object_owner 按 CLASSES 契约回填；重跑幂等（只增不删）

# 集成测试（真实 PG 5433）
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  .venv/bin/python -m pytest app/tests/integration/test_ontology_governance_integration.py -v
# → 9/9 PASS

# 全量回归 + 覆盖率
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  .venv/bin/python -m pytest app/tests/ --cov=app --cov-fail-under=80 -q
# → 1348 passed, TOTAL 93.23%
```

> 注：本次 dev 库（`qa_metadata`，5432）未在运行，真实 seed 的 dev 回填验证延后到环境就绪时执行；迁移 + 回填幂等已由集成测试在 `qa_metadata_test`（5433）真实 PG 上验证。

## 9. 真实数据验证（Harness 门禁）

集成测试全程走真实 PG 5433 + 完整 API 链路（client fixture 即 ASGI 测试客户端），seed 回填走 `seed_ontology.seed()` 真实函数 + 真实引擎（monkeypatch 仅替换引擎/Neo4j 同步为 no-op）。

### 9.1 验证结果（2026-08-30）

| 检查项 | 期望 | 实测 | 结论 |
|---|---|---|---|
| 迁移列 | ontology_class 含 object_type/object_owner | information_schema 确认 2 列 | ✅ |
| API 创建 | 携带 objectType/objectOwner 201 且读回一致 | `objectType:"Transaction"` / `objectOwner:"采购部"` | ✅ |
| API 校验 | 非法 objectType 拒绝 | 422 | ✅ |
| API 更新 | objectType/objectOwner 更新生效 | `Reference` / `数据治理组` | ✅ |
| 列表/详情 | 返回治理字段 | `Master` / `主数据管理组` 等 | ✅ |
| seed 回填 | 27 类与 CLASSES 契约一致 | 逐表断言 object_type/object_owner | ✅ |
| seed 幂等 | 重跑类数与字段值不变 | `PORDER→Transaction`、`ITMMASTER→Master` 稳定 | ✅ |
| 全量回归 | ≥80% 覆盖率 | 1348 passed / 93.23%（前端 321 passed） | ✅ |

### 9.2 数据契约 Roundtrip 一致性

枚举契约三层一致：`ObjectType` 枚举值（`Master`/`Transaction`/`Reference`/`Event`）＝ CLASSES dict 的 `object_type` 字符串值 ＝ DB 列存储值。单元测试验证 `model_dump(mode="json", by_alias=True)` 输出字符串值（非枚举成员名）且 camelCase key；集成测试验证 API 全链路（JSON→Pydantic→ORM→DB→JSON）Roundtrip 一致。

### 9.3 代码审查结果

**双 agent 并行审查（code-reviewer + security-reviewer）**：两者均 **APPROVE / APPROVED**，无 CRITICAL / HIGH 阻断项。

| Agent | 结论 | 发现 |
|---|---|---|
| code-reviewer | APPROVE | 2 MEDIUM（seed 覆盖语义、表单清空不落 null）；结构化核对全过（迁移链 0021→0022 对称、i18n key 齐全、枚举 roundtrip） |
| security-reviewer | APPROVED | 无 CRITICAL/HIGH/MEDIUM；3 LOW（信息级） |

**评审收敛点与修复**：

| # | 发现 | 级别 | 处理 |
|---|---|---|---|
| 1 | seed 回填 `!=` 条件会覆盖偏离契约的非 NULL 值（人工改过的治理字段在重跑 seed 时被校正回契约值） | MEDIUM(code-reviewer) / LOW(security) | **决策**：保持 `!=`（seed 是 27 张规范表的治理字段契约源，与 `_seedProperties` 对别名/描述的处理一致）；修正注释准确描述「校正偏离契约值 + 未提供字段保留」双重语义，消除注释与行为不一致 |
| 2 | 表单清空 objectType/objectOwner 后 `undefined` 被 JSON.stringify 丢弃 → PUT 省略字段 → `exclude_unset` 视为未修改 → 清空不生效（allowClear 暗示可清空但实际无效） | MEDIUM | **修复**：ClassTab 提交时治理字段映射 `values.objectType ?? null` / `values.objectOwner \|\| null`（显式 null 提交，后端落 NULL）；Create/Update 类型允许 `\| null`；新增集成测试 `test_update_clears_governance_fields_with_null` 锁定后端清空契约 |
| 3 | object_type 仅 API DTO 层白名单校验，无 DB CHECK 约束 | LOW | 接受现状（全部写路径均经枚举或受信常量；CHECK 约束留后续治理强化），记入已知缺口 |
| 4 | object_owner 无 min_length，接受空/空白字符串 | LOW | 接受现状（前端已归一空值为 null；API 边界一致性留待字段校验统一治理），记入已知缺口 |

**修复后验证**：Phase 新增测试 12 单测 + 9 集成 = **21/21 PASS**；后端全量 **1348 passed**（+1 清空测试），覆盖率 **93.23%**；前端 OntologyPage 25/25 PASS + `tsc --noEmit` 通过。

## 10. 关联

- 计划：`/Users/sunql/.claude/plans/mighty-mixing-sutherland.md` Phase 3.4
- 前置：`Harness/changes/feat-missing-business-objects/summary.md`（Phase 3.3）
- 模板：`Harness/changes/feat-missing-business-objects/summary.md`（10 段 SSOT）
- 下一阶段：`feat-entity-mapping-seed` 之后的 Phase 4（KPI Catalog / AI Feature Layer）
- 规则：`Harness/rules/开发流程规范.md`（10 阶段工作流）
