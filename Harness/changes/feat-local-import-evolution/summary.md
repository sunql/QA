# 变更：feat-local-import-evolution（本地数据初始化）

- **日期**：2026-09-09
- **作者**：启琳（Claude Code）
- **Phase**：本地数据初始化（本体 Ontology Init）— 演进现有导入
- **状态**：✅ 完成（#18 复核）。后端 36 例真 PG 绿、前端受影响套件全绿、tsc 0 错、目录内 funcs ≥80

## 1. 需求

THBI Oracle（1676 张表，27 张核心业务表）已接入，但 `ontology_class / ontology_property / ontology_join` 需通过 UI「本地数据初始化」填充。用户决策：**演进现有 `local_import` 导入**（不另建并行 ontology-init 服务），只补三块能力：

1. **自动 join 推断**：THBI 不声明任何 FK/PK → 用 Sage X3 列名约定推断引用边；声明外键照常推断
2. **单表部分列导入**：多表默认全列；单选一张表时可收窄到部分列
3. **关联关系重构**：增/删/改 ontology_join（复用 `/ontology/joins`）

## 2. 关键决策

| 决策 | 结论 |
|---|---|
| 演进 vs 新建 | **演进现有 local_import**（`/datasources/{id}/import-preview` + `/import`），不加并行服务 |
| 场景 C 落点 | **复用现有 OntologyPage →「关联」Tab（`JoinTab.tsx`，已含 list/create/delete + 类/列过滤）**，不新建 admin join 页、不动 seed_menu / section 计数（菜单 invariant 红线） |
| 推断双开关 | `ImportRuleConfig.join_inference.{infer_declared_fk, infer_name_convention}` 默认均开；前端向导默认透传 |
| 列选语义 | `selectedColumns` 只下发「真子集」（全列/超集/空不写）；空选 = 未收窄（与「不导入」用取消选表表达区分） |
| actor | execute 阶段全部走 keyword-only actor 契约：`createClass/createProperty/createJoin(..., actor=user.userId, actor_departments=...)`（修既有 bug：此前位置传参/漏传） |

## 3. 后端

- **`app/services/join_inference.py`**：`SAGE_X3_REFERENCE_MAP`（大写列名 → (目标表, 目标键)，如 `ITMREF_0→ITMMASTER`、`POHNUM_0→PORDER`、`BPRNUM_0→BPARTNER`、`BPSNUM_0→BPSUPPLIER`、`FCY_0→FACILITY` 等）+ `infer_sage_x3_name_convention_joins(tables) -> list[ProposedJoin]`。两端都 ∈ 传入 tables 才出边；self-ref/悬空目标/小写跳过；去重；`inferred_by="name_convention"`。
- **`app/services/local_import_service.py`**：`build_preview(..., selected_columns=None)`；join 生成抽到 `_build_joins`（声明 FK + 列名约定，`inferred_by` 标注 `declared_fk/name_convention`）；joined-table prune 需 BOTH 端点 ∈ selected_tables；`_pruneJoinsOnUnimportedColumns` 丢弃「被收窄列所在表」上引用未导入列的边；actor 修复。
- **`app/domain/schemas.py`**：`ProposedJoin.inferred_by`、`JoinInferenceRules`、`ImportRuleConfig.join_inference`、`ImportPreviewRequest.selected_columns`。
- API 路由不变：`POST /datasources/{id}/import-preview` / `/import`。

## 4. 前端

- `types/localImport.ts` + `types/datasource.ts`：schema/列/外键/推断规则/列选契约（camelCase 对齐）。
- `api/localImport.ts` `getImportPreview(id, request)` 发整请求；`api/datasource.ts` 新增 `getDatasourceSchema` / `introspectDatasource`。
- **`components/localImport/importSelection.ts`**（纯函数）：`tableNameIndex/allColumnNames/chosenColumns/toRulesWithJoinInference/pruneColumnSubset/toPreviewRequest/joinKey/validJoins/buildExecuteRequest`。
- **`RuleConfigStep.tsx`**（替换 stub）：schema 驱动选表（多选 + 搜索 + 全选筛选）+ 单选一张表时列 Checkbox 组（默认全列，收窄即真子集）+ join 推断双 Switch。
- **`ImportWizard.tsx`**：打开拉 schema（`GET /schema`，404 → `POST /introspect`），持有 `selectedTables/columnSubset`，选表变化即 prune 列选白名单，未选表禁用「下一步」。
- **`PreviewStep.tsx`**：类表 + join 表（每行 checkbox，`inferredBy` Tag：声明外键=蓝/列名约定=紫）；两端都选才可勾选，未选端点禁用；确认走 `buildExecuteRequest`。
- **`JoinTab.tsx`**（场景 C）：新增「仅显示外来键关联（自动推断）」开关（`relation_type=foreign_key` 聚焦核查机器推断边），不加新页面/菜单。
- **独立挂载页 `LocalImportInitPage.tsx`**（追加：用户点选「独立挂载页 /local-import」，见 §8）：菜单直达本地初始化，数据源 Select 默认 isDefault 优先 +「开始导入」弹复用 ImportWizard。
- i18n：`localImport.config.*`、`localImport.preview.*`（join 列/推断来源）、`localImport.initPage.*`、`toast.schemaLoadFailed`、`forms.ontology.joinOnlyForeignKeys*`、`menu.item.localImport`，zh/en 双语。

## 5. 测试

- 后端：`test_join_inference.py`（6 纯单测）、`test_local_import_join_inference.py`（7 service 假对象）、既有 `test_local_import_service.py`/`rule_engine`/集成 `test_local_import.py` 全绿。
- 前端：`importSelection.test.ts`（13）、`ImportWizard.test.tsx`（13，含列选真子集下发、join 启用/禁用断言、规则表搜索、关闭「列名约定」开关后请求携带 false）、`localImportApi.test.ts`（2）、`JoinTab.test.tsx`（13，+仅外来键 2 例）。
- 本目录模块 vitest 覆盖（#18 复核）：`components/localImport` + `JoinTab` 目录内单文件 funcs **全部 ≥80** — ConfirmStep 100 / importSelection 100 / RuleConfigStep 90.9 / PreviewStep 85.71 / ImportWizard 87.5 / JoinTab 88.88；目录聚合 95.88 stmts / 92.3 branch / 91.11 funcs / 95.88 lines。`tsc -b` 0 错。

## 6. 边界与注意事项

- **预存 bug（本特性 TDD 暴露并修复）**：`execute_import` 位置传 actor / join、property 漏传 actor → 真实集成测试 RED；已改 keyword-only 并修 fakes。
- 混合套件 81 失败为预存/跨套件（TRUNCATE 清掉 0039/0040 seed 类 + Neo4j/Milvus 外部设施），与本改动无关（见记忆 qa-system-mixed-suite-truncate-hazard）。
- `selectedColumns` 后端仅在表被 whitelist 时生效；未收窄表全列。
- 全量前端 functions 覆盖率 80% 门槛被其他 0% funcs 文件预存拉低（见记忆 qa-system-frontend-coverage-gate），#18 复核。

## 7. #18 复核证据（2026-09-09）

- **后端（真实 PG `qa_metadata_test`）**：`uv run pytest test_join_inference + test_local_import_join_inference + test_local_import_service + integration/test_local_import.py` → **36 passed**（Alembic 自动 upgrade head + 每例 TRUNCATE，覆盖 preview 列选、join 推断、actor 契约、幂等导入全链路）。无 uvicorn/容器依赖（httpx ASGITransport 进程内全链路）。
- **前端受影响套件**：importSelection 13 / ImportWizard 13 / localImportApi 2 / JoinTab 13 → 全绿；`npx tsc -b` 0 错。
- **全量 vitest（98 passed / 1 failed 文件）**：唯一失败文件 `ChatPage.test.tsx` 17 例 —— **预存基线**，单文件隔离复现，根因 `ChatPage → ChatPanel useLocation()` 缺 `<Router>` 上下文（`ChatPanel.tsx:46`），与本地导入改动无关（本特性未触碰 chat 路径）。另 `AgentRegistryPage.test.tsx` 1 例 unhandled-rejection teardown（其 5 例单跑全绿）亦为预存。
- **全量 `test:coverage` 未能产出 v8 报告**：run 在 AgentRegistryPage unhandled rejection / ChatPage Router 崩溃后中止，coverage 目录无产物、无文本表 → 全局数字本次无法复测；全局门槛预存缺口仍见记忆 qa-system-frontend-coverage-gate。目录级/文件级数字以上方定向 coverage（仅本次特性模块图）为准。
- **Playwright 浏览器 E2E 未跑**：真实栈 E2E 需后端容器承载本改动代码，而 qa-backend 容器先于本改动构建且重建受已知 infra 坑阻断（baked venv 缺 aiohttp / apt 源不可达，见记忆 qa-system-runbook-quirks）；后端集成测试已用真 PG + 完整 API 链路覆盖同等行为，浏览器层交互由 jsdom vitest 覆盖。
- 工作树改动未提交（用户未要求 commit）。

## 8. 追加：独立挂载菜单（用户后补需求，2026-09-09）

评审本无新菜单（场景 C 复用 JoinTab，菜单项/6-section 计数是测试红线）。用户后续要求「人工加菜单」，选定 **独立挂载页 `/local-import`**，落地：

- **前端**：`pages/LocalImportInitPage.tsx`（数据源 Select 默认 isDefault、按钮开复用 ImportWizard，`key` 换数据源重挂载）；`App.tsx` 加 `<Route path="local-import">`（挂 AppLayout 下）。
- **菜单接线**：`menuIcons.tsx` 注册 `import` 图标；i18n `menu.item.localImport`（zh/en）；菜单行由 seed 落库。
- **后端**：`seed_menu_config.py` ITEMS 加 `item.localImport`（section.foundation，sort 415，path `/local-import`，icon `import`）→ **6 section + 29 items = 35 行**（docstring 同步）；`test_seed_menu_config.py` 计数 34→35 / items 28→29、`frontend_routes` 加 `/local-import`（**路径对齐契约**：seed item.path 集合 == App 路由集合，强制双侧同步）。
- **测试**：`LocalImportInitPage.test.tsx` 5 例（默认选 isDefault、取首个、加载失败、空态、开-关-重开向导）；页面 funcs **100%**。受影响套件 75 例全绿；`tsc -b` 0 错。
- 运行时提示：seed 仅在「行不存在」时 INSERT；已存在的 `item.localImport` 由 AdminMenusPage 编辑。非 admin 用户需在 `/admin/menus` 授权勾选该 code 才可见（admin/stub 全可见）。
