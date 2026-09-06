# feat-dq-rule-auto-generation

> 日期：2026-09-05 | 状态：done | Spec: docs/superpowers/specs/2026-09-05-dq-rule-auto-generation-design.md
> Plan: docs/superpowers/plans/2026-09-05-dq-rule-auto-generation.md

## 目标

根据数据字典（列名/类型/描述/主键/外键/值域/关联）自动推导数据质量规则，
并支持 LLM 辅助生成（用户描述列的业务语义）。覆盖 COMPLETENESS / CONSISTENCY / VALIDITY / DICT_REF 四类维度。

## 实现

### 数据模型（Alembic 0017 / 0018）
- `data_quality_rule` 表（SSOT）：`id / name / dimension / derivation_type / expression /
  blocked_values / severity_order / enabled / version (乐观锁) / created_time / updated_time`
- `data_quality_rule_datasource` 表（N:M）：`rule_id (FK) / datasource_id (FK)`，组合唯一索引
- 唯一索引 `name`；`version` 每次 UPDATE 时 bump（乐观锁防并发覆盖）

### DTO / 枚举
- `DataQualityRuleDimension` enum：`COMPLETENESS / CONSISTENCY / VALIDITY / DICT_REF / TIMELINESS`
- `DerivationType` enum：`MANUAL / AUTO_GENERATED / LLM_SUGGESTED`
- `DataQualityRuleCreate` / `DataQualityRuleUpdate` / `DataQualityRuleRead`：
  - Create：必填 name/dimension/expression，可选 blockedValues/severityOrder
  - Update：必须含 `version`；name 不可改（422）
  - Read：含 id/version/audit/datasourceIds 字段
- `RuleSuggestion` / `GeneratePreviewResponse`（LLM 辅助 DTO）
- `ColumnMeta`（列元数据传递结构）

### Service + Engine
- `DataQualityRuleService`：list/get/create/update/delete，async session 包裹
- `DataQualityRuleGenerator`（引擎）：纯同步，无 IO，无 LLM
  - `CompletenessEvaluator`：`IS NULL` 率阈值 → HIGH/MEDIUM/LOW severity
  - `ConsistencyEvaluator`：DATETIME 列配对 + JOIN 同名列一致性；单表谓词，跨表用相关子查询
  - `ValidityEvaluator`：类型正则（文本物理列）；值域白名单；空值率阈值
  - `DictRefEvaluator`：外键列是否存在关联表；引用完整性
  - `ConstraintProvider` 接口：可扩展为方案 B（独立约束表）
- `DataQualityRuleGenerateService`：4-step 推导流程（schema 加载 → 规则生成 → 去重 → 持久化）
- `DataQualityRuleLLMService`：parse-descriptions AI 辅助（advisory-only，不写库）
  - 候选 `property_name` 注入 prompt（防 LLM 幻觉）
  - Pydantic output parse + 503 fail-loud

### REST API（`/api/v1/data-quality`，admin-only 写）
- `GET /` → list（`?dimension=` 过滤）
- `GET /{id}` → detail（404 NotFound）
- `POST /` → create（409 versionConflict / 422 validation）
- `PUT /{id}` → update（需 `version`，409 versionConflict）
- `DELETE /{id}` → delete
- `POST /generate` → 基于数据源自动生成规则
- `POST /generate/preview` → 预览生成结果（不持久化）
- ACL：读 getCurrentUser；写 getAdminOnlyActor

### 前端
- `frontend/src/types/dataQuality.ts`：DataQualityRule{Read,Create,Update} + dimension enum
- `frontend/src/api/dataQuality.ts`：6 个函数（CRUD + generate + preview），prefix = `/data-quality`
- `frontend/src/i18n/zh-CN.ts` + `en-US.ts`：`dataQuality.{title,columns,actions,form,messages,errors}` 命名空间
- `frontend/src/pages/DataQualityGeneratePage.tsx`（新）：Table + Create/Edit Modal，
  AI 辅助填写 modal（调用 `/data-quality/generate/preview`，advisory-only）
- `frontend/src/App.tsx`：注册 `/data-quality/generate` 路由
- `scripts/seed_menu_config.py`：新增 `item.dataQualityGenerate`（sort_order 325）

## 验证

### 单元（真实 PG，TRUNCATE 隔离）
- `test_data_quality_expression_whitelist.py`：表达式白名单覆盖
- `test_data_quality_evaluator_dispatcher.py`：维度分发路由
- `test_data_quality_evaluators.py`：各 Evaluator 覆盖率
- `test_data_quality_rule_generator.py`：引擎推导逻辑
- `test_chat_data_quality_dto.py`：DTO 契约

### 集成（真实 PG，TRUNCATE 隔离）
- `test_data_quality_api.py`：CRUD 端到端（404/409/422 断言）
- `test_data_quality_eval_api.py`：评估端到端
- `test_data_quality_score_api.py`：评分端到端
- `test_data_quality_score_audit.py`：审计写入
- `test_chat_data_quality_integration.py`：Chat 集成

### 覆盖率门槛
- `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
   uv run pytest app/tests/ --cov=app --cov-fail-under=80`

## 关键设计决策

- **COMPLETENESS 保留 PK**：主键列不生成 COMPLETENESS 规则（PK 本身保证非空）
- **表达式白名单**：仅允许 `IS NULL / IS NOT NULL / = / != / < / > / <= / >= / AND / OR / NOT / EXISTS / IN / LIKE / ~` 及圆括号；
  Task 2 前置：补充 `~ ^ $ { } :` 到白名单（CONSISTENCY 正则所需）
- **CONSISTENCY 跨表**：DATETIME 列配对 JOIN 同名列，相关子查询表达，表达式字符在白名单内
- **DICT_REF 去重**：ref 指向 Reference 类时只生成 DICT_REF VALIDITY，不再叠加 REFERENTIAL 规则
- **TIMELINESS 未做**：评估器未实现，LLM 描述解析也跳过 TIMELINESS 维度
- **纯同步 evaluator**：引擎无 IO 无 LLM，保证 evaluate 路径零额外延迟
- **advisory-only AI 辅助**：LLM 生成结果展示在 modal 让管理员确认，只读不写库
- **Registry warmUp 模式**：与 `AgentToolConfigRegistry` / `FeatureRuleRegistry` 一致；
  conftest autouse 必须 `dbSession.commit()` after warmUp

## 关联变更

- 前置：`feat-feature-rule-config`（warmUp 模式参考）
- 前置：`feat-agent-tool-config-db`（AgentToolConfigRegistry warmUp 参考）
- 后续（潜在）：`target_level="QUALITY_SCORE"` DataQualityRuleModel 迁移；
  TIMELINESS 评估器实现；方案 B 独立约束表升级路径

## Out of scope

- TIMELINESS 维度评估器
- 方案 B（独立约束表）升级路径实现（引擎 ConstraintProvider 接口已预留薄接口抽象）
- Compound expressions (AND/OR trees)
- Redis 多实例缓存

## 遗留项

- **TIMELINESS 维度未实现**：评估器未做，LLM 描述解析也跳过该维度
- **方案 B 升级路径**：ConstraintProvider 接口已在 `data_quality_rule_generator.py` 内薄接口抽象，
  可后续升级为独立约束表存储
- **类型正则仅覆盖文本物理列**：DATETIME / INT / BOOLEAN / DECIMAL 物理类型做正则；
  非文本列跳过类型正则推导
- **DICT_REF 去重**：ref 指向 Reference 类时只生成 DICT_REF VALIDITY，不叠加 REFERENTIAL（防重复规则）
- **blocked reason 未含具体命中关键字**：黑名单保守阻断合法值域中的保留字（如含 DELETE/UNION 的值），
  reason 仅返回被阻断，不含具体命中的关键字
- **service 文件行数**：部分 service 文件超过 400 行建议（最大 426 行），在 800 行上限内
- **parse-descriptions 未显式记录 token 计量**：fake client 路径不计量，依赖 OpenAiClient.complete()
  → LlmResponse 真实链路覆盖
