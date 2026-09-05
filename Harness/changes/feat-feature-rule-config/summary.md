# feat-feature-rule-config

> 日期：2026-09-05 | 状态：done | Spec: docs/superpowers/specs/2026-09-05-feature-rule-config.md
> Plan: docs/superpowers/plans/2026-09-05-feature-rule-config.md

## 目标

把硬编码 RISK_RULES + DEFAULT_SUPPLIER_FEATURES 迁到 DB-backed 通用 Feature 规则引擎，
业务用户可改阈值无需发版；后续 Phase 6 Agent 平台可演进多 target_level。

## 实现

### 数据模型（Alembic 0043）
- `feature_rule` 表（SSOT）：`id / name / description / target_level / expression /
  severity_order / enabled / version (乐观锁) / created_time / updated_time`
- `feature_rule_threshold` 表（1:N）：`id / rule_id (FK) / level / upper_threshold /
  created_time / updated_time`
- 唯一索引 `name`；`version` 每次 UPDATE 时 bump（乐观锁防并发覆盖）

### DTO / 枚举
- `FeatureRuleThresholdLevel` enum：`HIGH / MEDIUM / LOW / UNKNOWN`
- `FeatureRuleCreate` / `FeatureRuleUpdate` / `FeatureRuleRead`：
  - Create：必填 name/targetLevel/expression/severityOrder，可选 description/thresholds
  - Update：必须含 `version`；name 不可改（422）
  - Read：含 id/version/audit 字段
- `FeatureRuleThresholdCreate` / `FeatureRuleThresholdRead`

### Service + Registry
- `FeatureRuleService`：list/get/create/update/delete，async session 包裹
- `_FeatureRuleInUseConflict(Error)`：删除时若 Supplier360Service 仍引用该 rule → 409
- `FeatureRuleRegistry`：模块级缓存，DB 是 SSOT
  - `warmUp(session)`：启动预热（lifespan 调用）
  - `reloadAll/reloadOne`：写时失效（service 写完后调用）
  - `asyncio.Lock` 防止并发 reload 竞态
  - `get(rule_name) / getAll() / getByTargetLevel(target_level)` 同步接口
- `FeatureRuleEvaluator`：纯同步，无 IO，无 LLM
  - `evaluate(record, rule_name, extra_context)` → `FeatureRuleResult(severity, detail)`
  - 跨规则聚合：MAX severity（`min(SEVERITY_ORDER)` 映射到排序值）
- `feature_rule_llm_service`：AI 辅助 expression 生成（advisory-only，不写库）
  - 候选 `feature_name` 注入 prompt（防 LLM 幻觉）
  - Pydantic output parse + 503 fail-loud

### Runtime 集成
- `SupplierRiskService._decideLevel_via_rules`：4-step RISK-priority bypass 包装
  1. RISK_SCORE 规则命中 → 对应 threshold level（字节级兼容 legacy）
  2. 全部 feature 缺失 → UNKNOWN
  3. 取其余（非 RISK_SCORE）规则最大 severity
  4. 无匹配 → LOW（默认）
- `Supplier360Service._safeLoadKpis`：从 `FeatureRuleRegistry.getByTargetLevel("SUPPLIER")`
  聚合所有 `SUPPLIER` target_level 的 feature_name（含未来扩展点）

### REST API（`/api/v1/feature-rules`，admin-only 写）
- `GET /` → list（`?targetLevel=SUPPLIER` 过滤）
- `GET /{name}` → detail（404 NotFound）
- `POST /` → create（409 versionConflict / 422 validation）
- `PUT /{name}` → update（需 `version`，409 versionConflict）
- `DELETE /{name}` → delete（409 referencingAgents）
- ACL：读 getCurrentUser；写 getAdminOnlyActor

### seed（启动幂等）
- `scripts/seed_feature_rules.py`：4 条内置规则幂等 upsert
  - `RISK_SCORE_HIGH`：`expression="risk_score >= 0"`，thresholds: HIGH=0.60 / MEDIUM=0.80 / LOW=1.01
  - `RISK_SCORE_MEDIUM` / `RISK_SCORE_LOW`（冗余兼容，已被 HIGH 覆盖）
  - `FINANCIAL_HEALTH_DEFAULT`
  - LOW tier `upper_threshold=1.01`：值域 [0,1] 永远命中，保证 RISK_SCORE ≥ 0.80 时 bypass 返回 LOW

### 前端
- `frontend/src/types/featureRule.ts`：FeatureRuleConfig{Read,Create,Update} + FeatureRuleThresholdLevel enum
- `frontend/src/api/featureRules.ts`：5 个函数（list/get/create/update/delete），prefix = `/feature-rules`
- `frontend/src/i18n/zh-CN.ts` + `en-US.ts`：`featureRules.{title,columns,actions,form,messages,errors}` 命名空间
- `frontend/src/pages/AdminFeatureRulesPage.tsx`（新）：Table + Create/Edit Modal +
  Delete Popconfirm，AI 辅助填写 modal（调用 `feature_rule_llm_service`，advisory-only）
- `frontend/src/App.tsx`：注册 `/admin/feature-rules` 路由

## 验证

### 集成（真实 PG，TRUNCATE 隔离）
- `test_feature_rule_supplier_risk_parity.py`：8 场景证明 byte-identical 兼容 legacy `_decideLevel`
  - RISK_SCORE 0.85 → MEDIUM；0.60 → HIGH；0.55 → MEDIUM
  - all-missing → UNKNOWN
  - no-match → LOW
  - max(others) 逻辑

### 覆盖率门槛
- `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
   uv run pytest app/tests/ --cov=app --cov-fail-under=80`

### 前端
- `frontend/src/tests/AdminFeatureRulesPage.test.tsx`
- `frontend/src/tests/featureRules.test.ts`

## 关键设计决策

- **3-tier RISK_SCORE seed**：HIGH 0.60 / MEDIUM 0.80 / LOW 1.01；LOW tier 1.01
  "always-hit" 保证字节级 parity——值域 [0,1] 命中，RISK_SCORE ≥ 0.80 → bypass 返回 LOW
- **纯同步 evaluator**：`FeatureRuleEvaluator.evaluate(...)` 聚合 MAX severity，无 IO / 无 LLM，
  保证 evaluate 路径零额外延迟
- **RISK-priority bypass wrapper**：`_decideLevel_via_rules` 是 legacy 逻辑 guard，
  外部调用方（`SupplierRiskService.decideLevel`）无需感知 DB 规则存在——向后兼容
- **advisory-only AI 辅助**：LLM 生成结果展示在 modal 让管理员确认，只读不写库；
  候选 feature_name 注入防幻觉；Pydantic parse 失败 → 503
- **Registry warmUp 模式**：与 `AgentToolConfigRegistry` 一致；conftest autouse 必须
  `dbSession.commit()` after warmUp（commit d01a4e1 教训：AccessShareLock 阻塞 TRUNCATE）
- **`target_level` 扩展点**：`target_level="QUALITY_SCORE"` 字段保留，供后续
  DataQualityRuleModel 接入（当前 `DataQualityRuleModel` 内部 evaluator 未迁移）

## 关联变更

- 前置：`feat-agent-tool-config-db`（AgentToolConfigRegistry warmUp 模式参考）
- 前置：`feat-supplier-risk`（`_decideLevel` legacy 逻辑已稳定）
- 后续（潜在）：`target_level="QUALITY_SCORE"` DataQualityRuleModel 迁移；
  compound expressions (AND/OR trees)；Redis 多实例缓存

## Out of scope

- `DataQualityRuleModel` 内部 evaluator 迁移（`target_level="QUALITY_SCORE"` plumbing reserved）
- Compound expressions (AND/OR trees)
- Redis 多实例缓存
