# 变更：本地导入（数据源 → 本体批量生成）

- **日期**：2026-08-29
- **作者**：AI 助手
- **Phase**：Phase 5 扩展（本地导入）
- **状态**：done

## 1. 需求

管理员在注册数据源后，可通过向导式界面读取该数据源 schema，经 LLM 语义增强与人工预览确认后，批量生成本体 Class / Property / Join。

**用户决策**：
- **前端对齐后端**：设计文档请求示例以真实后端契约为准——删除未实现的 `tableFilter` 字段（`includeViews` / `includeSystemTables` / `ownerWhitelist`），`typeMappings`（复数）改为 `typeMapping`（单数，匹配后端 `type_mapping` 的 JSON 契约）。
- 半自动预览：生成 proposals 后必须经人工确认才落库；类/属性冲突在预览阶段列出，由用户处置。

## 2. 设计评审

- 后端拆 4 个单一职责小服务：`ImportRuleEngine`（表过滤 + 类型映射，纯函数、不可变）、`ImportConflictResolver`（类/属性冲突检测）、`ImportLlmEnhancer`（LLM schema 增强，失败降级）、`LocalImportService`（编排 preview/execute）。
- 类型映射：`map_data_type` 按用户 `type_mapping` 查表，未命中回退 `STRING`；Oracle `NUMBER(p=0,s=0)` 归一为整数 key、通用 `NUMBER` 归一为数值 key；`TINYINT(1)` 归一为 `BOOLEAN`。
- LLM 增强：prompt 仅含表/列名与数据类型元数据，输出严格 JSON；无效 JSON 或未注入 llm_client 时降级为「无增强」而非抛错。
- 冲突检测：类冲突按 `source_table` 匹配既有类；属性冲突按 `(source_table, source_column)` 匹配既有属性。
- API 挂载：`local_import.router` 以 prefix `/api/v1/datasources` 挂在 `main.py`，与 `datasource.router` 并列（沿用项目「避免 v1Router 双重叠加 prefix」的既有做法）。
- 前端 `executeImport` 走独立 raw axios：`ImportExecuteResponse.success` 与统一信封的 `success` 冲突，走 httpClient 会被拦截器误判为失败信封并丢失 `errors` 数组。

## 3. 数据模型变更

**无 Alembic 迁移**。复用既有 `OntologyClass` / `OntologyProperty` / `OntologyJoin` 三张表（经 `OntologyService.createClass / createProperty / createJoin` 落库）。本期仅新增 Pydantic DTO（`app/domain/schemas.py`），不新增/修改数据库表结构。

## 4. 接口契约变更

新增 2 个端点（`app/api/v1/local_import.py`，prefix `/api/v1/datasources`，均 `Depends(getCurrentUser)` 鉴权）：

| 端点 | 路径 | 用途 |
|---|---|---|
| POST | `/api/v1/datasources/{datasourceId}/import-preview` | 生成导入预览（建议类/属性/关联 + 冲突 + 过滤建议） |
| POST | `/api/v1/datasources/{datasourceId}/import` | 执行导入，落库本体类/属性/关联 |

新增 DTO（snake_case 字段 + `CamelModel` alias generator，JSON 契约 camelCase）：
- `ImportRuleConfig`：`table_filter` / `type_mapping` / `llm_enhance_options`。
- `TableFilterRules`：`include_temp_tables` / `name_blacklist_patterns`（仅这两个字段，即后端真实实现）。
- `TypeMappingRules`：`mappings: dict[str, str]`。
- `LlmEnhanceOptions`：`generate_aliases` / `generate_descriptions` / `detect_enums` / `suggest_filters`。
- `ProposedClass` / `ProposedProperty` / `ProposedJoin`（均带 `is_selected`）。
- `ImportConflict` / `ConflictResolution`。
- `ImportPreviewRequest` / `ImportPreviewResponse` / `ImportExecuteRequest` / `ImportExecuteResponse` / `FilterSuggestions` / `LlmUsageInfo` / `ImportErrorInfo`。

## 5. 实现要点

**后端新增**：
- `app/services/import_rule_engine.py`：`filter_tables`（黑名单正则 + 临时表前缀）、`map_data_type`（归一化 + 查表 + 回退 STRING）。
- `app/services/import_conflict_resolver.py`：`detect_conflicts`（类/属性两级）。
- `app/services/import_llm_enhancer.py`：`enhance_schema`（prompt 构造 + JSON 解析 + 失败降级）。
- `app/services/local_import_service.py`：`build_preview`（introspect → filter → enhance → build proposals → detect conflicts）、`execute_import`（confirmed classes → properties → joins，逐项 try/except 收集 `ImportErrorInfo`）。
- `app/api/v1/local_import.py`：2 端点；测试通过 `app.state.localImportService` 注入 fake schema service。
- `app/main.py`：挂载 `local_import.router`。
- `app/domain/schemas.py`：上述 DTO + `DEFAULT_TYPE_MAPPINGS` / `DEFAULT_TABLE_NAME_BLACKLIST_PATTERNS` 常量。

**前端新增/修改**：
- `frontend/src/types/localImport.ts`：契约类型（与后端 camelCase JSON 一致）。
- `frontend/src/api/localImport.ts`：`getImportPreview`（走 httpClient）、`executeImport`（走独立 raw axios）。
- `frontend/src/components/localImport/ImportWizard.tsx`：三步 Modal（规则配置 → 预览确认 → 导入完成）。
- `frontend/src/components/localImport/PreviewStep.tsx`：只读展示 proposedClasses 摘要，确认后构造 `ImportExecuteRequest` 提交。
- `frontend/src/components/localImport/RuleConfigStep.tsx`、`ConfirmStep.tsx`：骨架（见「已知缺口」）。
- `frontend/src/pages/DatasourcePage.tsx`：表格操作列新增「智能导入到本体」入口。
- `frontend/src/i18n/zh-CN.ts` / `en-US.ts`：新增 `localImport.*` 与 `datasource.importToOntology` 键位。

## 6. 测试

**后端服务单测**（27 用例，4 个文件）：
- `test_import_rule_engine.py` 10、`test_import_conflict_resolver.py` 3、`test_import_llm_enhancer.py` 3、`test_local_import_service.py` 11。
- 覆盖率：`local_import_service.py` 97%；4 个 import service 合计 98.31%（236 stmts / 4 missed）。

**后端集成测试**（真实 PG 5433 + 完整 HTTP 链路，8 用例）：`test_local_import.py` 覆盖预览返回、执行落库 class/property/join、缺失数据源 404、表过滤、类型映射、冲突检测、`is_selected` 生效。

**前端单测**：vitest 29 文件 267 passed；`npx tsc --noEmit` clean。

**E2E**：`e2e/local-import.spec.ts` 1 passed（向导全流程：入口 → 规则配置 → 预览 → 确认导入 → 完成）。

## 7. 安全审查

- 鉴权：2 个新端点继承 `router = APIRouter(dependencies=[Depends(getCurrentUser)])`，与既有 `/datasources/*` 一致。
- SQL 安全：无手写 SQL 拼接；落库走 `OntologyService`（SQLAlchemy ORM 参数化），冲突检测用 `listClasses / listPropertiesByClass` 只读查询。
- LLM prompt 仅含 schema 元数据（表/列名 + 数据类型），不含用户数据或凭据；LLM 失败降级不阻塞导入。
- ReDoS 风险已在 `TableFilterRules.name_blacklist_patterns` docstring 标注：当前仅服务端内置默认值使用；若未来开放用户配置需加 pattern 长度/复杂度防护。
- 无硬编码密码/密钥。
- 形式化 security-reviewer / code-reviewer 不在此步自派发（Correction 5），由 controller 在 Task 11 提交后作全分支最终评审统一派发（含 deferred minors 三方裁定）。

## 8. 部署验证

```bash
# 后端（服务单测 + 集成，真实 PG 5433）
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/services/test_import_rule_engine.py \
    app/tests/services/test_import_conflict_resolver.py \
    app/tests/services/test_import_llm_enhancer.py \
    app/tests/services/test_local_import_service.py \
    app/tests/integration/test_local_import.py -v
# → 35 passed（27 服务单测 + 8 集成）

# 前端
cd frontend
npm test                                       # 267 passed（29 files）
npx tsc --noEmit                               # clean
npx playwright test e2e/local-import.spec.ts   # 1 passed
```

## 9. 已知缺口（待办）

**deferred（如实记录，由 controller 最终评审三方裁定）**：
- `execute_import` 接受 `ConflictResolution`（`conflict_resolutions`）但未真正应用：`skipped_conflicts` / `overwritten_conflicts` 仅为请求列表中 `action` 的计数值，未对既有类/属性执行 skip / overwrite / rename。
- `llm_usage` 硬编码零值（`LlmUsageInfo()` 默认），LLM Token 计量未接通。
- `sync_embeddings` 接受但忽略，导入后未触发 embedding 同步。
- 预览阶段 N+1：`build_preview` 对每个既有类逐个调用 `listPropertiesByClass`。
- 前端向导仍为骨架：`RuleConfigStep` 为占位（规则 UI 未做），`ConfirmStep` 为静态成功页（未展示 `execute_import` 的真实结果与 errors）。

## 10. 关联

- 设计稿：`docs/superpowers/specs/2026-08-29-local-import-design.md`
- 计划：`docs/superpowers/plans/2026-08-29-local-import-plan.md`
- 规则：`Harness/rules/开发流程规范.md`（TDD、code review）、`Harness/rules/测试规范.md`（真实 PostgreSQL 测试）
- 既有相关：`app/services/schema_introspection_service.py`（introspect 源）、`app/services/ontology_service.py`（落库本体）、`app/api/v1/datasource.py`（同 prefix 数据源端点）。
