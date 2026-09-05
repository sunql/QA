# 数据质量规则自动生成 — 设计文档

- 日期：2026-09-05
- 分支：feat/business-object-registry
- 状态：已获用户批准的设计（两节确认通过）

## 1. 目标

从本体（ontology_class）导入一个类，读取其属性定义（结构化元数据 + 物理schema），自动生成默认的数据质量规则（完整性/唯一性/引用性/有效性/跨表一致性），经预览确认后落入 `data_quality_rule`，供后续数据质量管理（评估/评分）使用。支持重复生成幂等（来源标记 + 跳过已存在）。

## 2. 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| 字典依据 | 结构化字段为准 + LLM 解析 description 补充建议 |
| 入库流程 | 预览 → 人工确认 → 落库 |
| 入口 | 任意 ontology_class（不限 business_object） |
| 目标表/数据源 | 向导选数据源 + schema_cache 物理映射校验 |
| 维度覆盖 | 完整性/唯一性/引用性/有效性 + 跨表一致性（ontology_join）；TIMELINESS 不做（评估器未实现） |
| 幂等 | 规则带来源标记，确定性 rule_code 去重，跳过已存在，不动用户手改规则 |
| 方案选型 | 方案 A（轻量元数据扩展 + 纯函数推导引擎 + LLM 建议沉淀闭环），预留升级方案 B（独立约束表）的路径 |

## 3. 架构与数据流

```
用户（向导页）
  │ ① 选 ontology_class
  │ ② 选 datasource → schema_cache 拉物理表/列做映射校验
  ▼
RuleSuggestionEngine（纯函数，无 IO）
  │  输入：class + properties + 约束视图 + schema 映射结果
  │  输出：规则建议清单（rule_type / rule_expression / threshold /
  │        severity / 来源属性 / 推导理由 / 置信度 / 状态）
  ▼
预览页（可编辑阈值/severity，可勾选）
  │    └─ LLM advisory（旁路）：解析 description → 候选字典/范围建议
  │       确认后写回 ontology_property（沉淀为结构化元数据）
  ▼
确认落库 → data_quality_rule（幂等：确定性 rule_code 去重，跳过已存在）
  ▼
复用现有 DataQualityEvaluatorDispatcher + DataQualityScoreService（本期不动）
```

### 新增组件（全部小文件）

| 组件 | 位置 | 职责 |
|---|---|---|
| `ConstraintProvider` | `backend/app/services/data_quality_rule_generator.py` 内薄接口 | 从本体/schema 装配"属性约束视图"；升级方案 B 时换实现，引擎不动 |
| `RuleSuggestionEngine` | 同上（同文件，<300 行） | 纯函数推导 |
| `DataQualityRuleGenerateService` | `backend/app/services/data_quality_rule_generate_service.py` | 编排：拉类+属性、schema 校验、幂等落库、审计 |
| LLM advisory | `backend/app/services/data_quality_rule_llm_service.py` | 复用 feature_rule_llm_service 模式：解析 description → 候选约束建议，只建议不落库 |

## 4. 数据模型（Alembic 0044，单次迁移）

### ontology_property 新增 1 列

- `allowed_values JSONB NULL` — 值域型字典（如 `["NEW","CONFIRMED","CLOSED"]`）
- 表引用型字典复用已有 `ref_class_id`（被引用类 object_type=Reference 即字典表）

### data_quality_rule 新增 3 列（全部 nullable，存量零破坏）

- `source_class_id BIGINT NULL FK→ontology_class.id`
- `source_property_id BIGINT NULL FK→ontology_property.id`
- `derivation_type VARCHAR(32) NULL` — 取值：`PK_DERIVED / FK_DERIVED / DICT_REF / ALLOWED_VALUES / NOT_NULL / JOIN_CONSISTENCY / LLM_DERIVED / MANUAL`（存量回填 MANUAL）

## 5. 推导映射表（引擎核心逻辑）

| 元数据信号 | 规则类型 | 置信度 | 默认表达式（示例） |
|---|---|---|---|
| 物理列 NOT NULL（schema_cache） | COMPLETENESS | HIGH | `COL IS NOT NULL` |
| `is_primary_key` | UNIQUENESS | HIGH | `COL`（重复计数） |
| `is_foreign_key` + `ref_class_id` | REFERENTIAL | HIGH | `REF <ref.source_table>.<ref.source_column>` |
| `ref_class_id` 指向 object_type=Reference 类 | VALIDITY | HIGH | `COL IN (SELECT dict_col FROM dict_table)` |
| `allowed_values` 非空 | VALIDITY | HIGH | `COL IN ('A','B','C')` |
| `data_type` 与 schema 物理类型不匹配 | VALIDITY（类型可转性） | MEDIUM | 如 DATETIME 列 `COL::timestamp IS NOT NULL` |
| `ontology_join` 映射（日期/数量比较语义） | CONSISTENCY | MEDIUM | 跨表比较表达式 |
| LLM 从 description 识别字典/取值范围 | 建议进预览；确认后写回 allowed_values（沉淀元数据）再生成 | LOW→确认后 HIGH | — |
| LLM 从 description 识别业务必填 | 直接作为 COMPLETENESS 建议（derivation_type=LLM_DERIVED，不沉淀元数据，本体无 nullable 字段） | LOW | `COL IS NOT NULL` |

要点：**必填判定来自物理 schema 的 NOT NULL（确定性）**；本体侧业务必填由 LLM 建议补充。

### Schema 映射校验（预览阶段）

选定 datasource 后用 schema_cache 校验每个属性的 source_table/source_column 是否物理存在。三态：
`matched`（生成规则）/ `missing_column` / `missing_table`（建议标记 BLOCKED，不落库，预览页显示原因）。属性 source_column 为空 → BLOCKED（"未配置物理列映射"）。

## 6. API 契约

新路由文件 `backend/app/api/v1/data_quality_generate.py`，前缀 `/api/v1/data-quality/rules/generate`：

| 端点 | 方法 | 说明 |
|---|---|---|
| `/preview` | POST | body `{classId, datasourceId}` → 类信息 + 属性 schema 映射结果 + 规则建议清单（`status: NEW / EXISTS / BLOCKED`、`derivationType`、`confidence`、`reason`） |
| `/confirm` | POST | body `{datasourceId, rules: [勾选建议（可改 threshold/severity）]}` → `{created, skipped}`；批量落库，已存在 code 跳过 |
| `/parse-descriptions` | POST | body `{classId}` → LLM 候选约束建议（只建议不落库；503 fail-loud 但不影响确定性建议） |
| `/apply-suggestion` | POST | body `{propertyId, allowedValues: [...]}` → 确认的 LLM 建议写回 ontology_property.allowed_values（仅值域型；业务必填类建议不写回，直接走 confirm 以 LLM_DERIVED 落库） |

- **ACL**：preview/parse 读侧 `getCurrentUser`；confirm/apply 与现有 createRule 对齐（getCurrentUser + AclService，owner 从 actor 派生），不做 admin-only。
- **审计**：confirm 批量写规则走 OutboxService 模式，每条规则一条审计记录。
- **rule_code**：`DQ_<CLASS>_<PROP>_<TYPE>` 确定性生成；超长截断 + 短哈希后缀防撞。
- **Token 计量**：parse-descriptions 走现有 token_counter，强制记录（核心约束 3）。

## 7. 前端向导

`frontend/src/pages/DataQualityRuleGeneratePage.tsx`，路由 `/data-quality/generate`，**独立菜单入口**（不挂在 DataQualityPage 内）。

菜单接入（menu_config DB 驱动，需三处同步）：

- `backend/scripts/seed_menu_config.py` ITEMS 列表新增 `item.dataQualityGenerate`，parent 与数据质量项同分组，`sort_order` 紧随其后，`label_key = "menu.item.dataQualityGenerate"`，幂等 `on_conflict_do_update`
- i18n：zh-CN / en-US 双份 `menu.item.dataQualityGenerate`（命名空间用 `menu.item`，非 `appLayout.menu`）
- `frontend/src/components/common/fallbackNav.ts` 离线 fallback 同步

1. **选类**：本体类下拉（复用 ontology API）
2. **选数据源 + 映射校验**：数据源下拉（复用 data_source API）→ 属性↔物理列映射三态表
3. **预览规则**：建议清单表格，可编辑 threshold/severity、可勾选；EXISTS 行标灰；LLM 建议区（按属性确认 → 写回元数据后重新预览）
4. **确认**：落库结果页（created/skipped/blocked 统计）

- 新文件：`pages/DataQualityRuleGeneratePage.tsx` + `types/dataQualityGenerate.ts` + `api/dataQualityGenerate.ts`
- i18n：zh-CN / en-US 双份 `dataQualityGenerate` 命名空间

## 8. 错误处理

| 场景 | 行为 |
|---|---|
| classId 不存在 | 404 |
| 类无属性 | 200 + 空建议 + 提示"先维护本体属性" |
| schema_cache 无该数据源 | 全部 BLOCKED + 原因"数据源 schema 未缓存" |
| LLM 不可用 | parse-descriptions 503，向导可继续走确定性建议 |
| confirm 撞唯一约束（并发重复提交） | 捕获 IntegrityError → 该条记入 skipped，不失败整个批次 |
| 属性 source_column 为空 | BLOCKED + 原因"未配置物理列映射" |

## 9. 测试策略（真实 PG + HTTPX 完整 API 链路）

- **单元（纯函数引擎）**：表驱动覆盖推导映射表全部行 + 边界（PK 兼字典、无 source_column、超长 code 截断）
- **集成**：
  - preview：三类映射状态 + EXISTS 幂等标记
  - confirm：批量落库、重复 confirm 跳过、owner 派生、审计 outbox 落行
  - parse-descriptions：LLM fake 注入（复用现有 fakes helper 模式）+ token 计量断言
  - apply-suggestion：写回 ontology_property 后再次 preview 变为确定性 VALIDITY
- 覆盖率门槛 ≥ 80%（现有 baseline ~93%）

## 10. 升级路径（方案 A → 方案 B）

- 推导引擎只消费 ConstraintProvider 装配的"属性约束视图"，升级 B 仅换实现（从 ontology_property_constraint 表读取），引擎与已生成规则不动。
- 规则溯源三列与存储无关。
- 数据迁移：allowed_values JSONB 拆行入约束表，单向简单。
- 届时需扩展 rule_code 生成规则（属性+约束 id），本期不引入。

## 11. 明确不做（YAGNI）

- TIMELINESS 维度（评估器未实现）
- 调度/定时执行（沿用户既有决策：手动触发）
- DataQualityScoreService / 评估器改动
