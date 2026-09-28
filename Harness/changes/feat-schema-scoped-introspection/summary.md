# 变更：feat-schema-scoped-introspection（数据源 schema / Oracle owner 选择）

- **日期**：2026-09-09
- **作者**：启琳（Claude Code）
- **Phase**：数据源内省按 Oracle owner 作用域化 + 导入向导「选择 Schema」步
- **状态**：✅ 完成（代码 + 真实 PG 测试 + 容器部署 + 运行时 E2E）。工作树改动未提交（用户未要求 commit）。

## 1. 需求

本地数据初始化向导（`ImportWizard`）选定数据源后直接平铺**单个 owner** 的表。`THBI Oracle`（连接用户 `ZJTH`）因此只见 1676 张 X3 表，而数仓表（`THBI.DWD_* / DIM_* / ADS_*`，owner 是 **THBI**）从未可见、不可导入。

用户需求：**确定数据源后，先选择 schema（Oracle owner 命名空间），再看该 schema 下的表**。此前无法选择。

## 2. 关键决策

| 决策 | 结论 |
|---|---|
| schema 语义 | Oracle owner 命名空间（`ALL_TABLES` 的 `DISTINCT owner`）；PG/MySQL 无显式多 schema → 后端返回 `[]`，向导**不显示** schema 步，走连接默认 |
| 选择位置 | `ImportWizard` 内动态插入「选择 Schema」步（多 owner 时 4 步，否则维持原 3 步，索引经 `ruleIdx/previewIdx/confirmIdx` 归一） |
| 自动选首个 | **否**——不自动选 `SYS` 等系统 owner；用户显式选。加载失败复位到占位态允许重试 |
| 导入命名 | 本体 `source_table` 仍存**裸表名**（沿用 NL2SQL `schemaPrefix=ds.username` 机制）；schema 只决定表来源（缓存键），不改命名 |
| 缓存键 | `(datasource_id, schema_name)` 复合键；owner **先归一**（Oracle strip+upper+白名单；非 Oracle 恒 `''`）再入键与 SQL，杜绝同 owner 大小写不同产生重复行 |

## 3. 后端

- **`app/domain/models.py`**：`SchemaCache.schema_name`（String(100) NOT NULL DEFAULT ''）；唯一约束 `uq_schema_cache_datasource` → 复合 `uq_schema_cache_datasource_schema(datasource_id, schema_name)`。
- **迁移 `alembic/versions/0048_schema_cache_schema_scope.py`**：加列（server_default ''）→ 存量 Oracle 行回填 `UPPER(TRIM(username))` → 换复合唯一键（原单列唯一保证回填无冲突）。
- **`app/services/schema_introspection_service.py`**：
  - `_normalizeSchemaName(ds, owner)`（新增，替换 `_defaultSchemaName`）：**owner 在缓存键与 SQL 的单一归一入口**（Oracle `strip().upper()+[A-Z0-9_$#]` 白名单、空/缺省 → `UPPER(username)`；PG/MySQL 忽略 owner 恒 `''`）。
  - `listSchemas(ds)`：Oracle → `_ORACLE_SCHEMAS_SQL` distinct owner（静态只读、Python 侧白名单过滤）；PG/MySQL → `[]`。
  - `introspect/getCached/introspectAndCache(..., owner)`：键/SQL 均走 `_normalizeSchemaName`；并发 IntegrityError 撞复合键回滚复用胜出者。
  - `validateOntologyDrift`：仅查默认 owner 缓存（NL2SQL 默认 schema 语义，刻意保留）。
- **`app/domain/schemas.py`**：`ImportPreviewRequest.schema_name`（alias `schema`，避开 `BaseModel.schema` 冲突；JSON 契约仍是 `schema`）。
- **`app/api/v1/datasource.py`**：新增 `GET /datasources/{id}/schemas`（→ `list[str]`）；`POST /introspect` 与 `GET /schema` 增可选 `?schema=`。
- **`app/api/v1/local_import.py` / `app/services/local_import_service.py`**：`importPreview`/`build_preview(..., schema)` → `introspectAndCache(owner=schema)` 内省正确 owner；`source_table` 裸表名。

## 4. 前端

- `types/localImport.ts`：`ImportPreviewRequest.schema?: string | null`。
- `api/datasource.ts`：`listDatasourceSchemas(id)`；`getDatasourceSchema(id, schema?)` / `introspectDatasource(id, schema?)`（有 schema 才挂 params）。
- **`components/localImport/ImportWizard.tsx`**：`schemas: string[]|null` + `schemasFailed` + `selectedSchema`；三步加载门控（列表加载中整窗 Spin；失败 Alert+重试早退；空列表 → 跳过 schema 步直接默认 owner）；动态步数组；`handleSchemaSelect` 换 owner 清空表/列/预览再重载，**加载失败复位选择到占位态**（Select value 复位，保证「重选同一 owner」是一次新选择以触发重试）；schema 步「下一步」同时门控 `selectedSchema && schemaReady`（防进入空表步）；`handlePreview` 带 `schema: selectedSchema ?? undefined`。
- **`components/localImport/SchemaStep.tsx`**（新）：`data-testid="schemaSelect"` 的 antd Select（showSearch）。
- i18n（zh/en）：`localImport.steps.schema`、`localImport.schema.{loading,loadFailed,selectLabel,placeholder,ownerCount,hint}`、`common.retry`。

## 5. 测试

- 后端（真实 PG `qa_metadata_test`，改后 **71 passed**，无 sqlite）：owner 归一/白名单/内省透传、复合键并发、`listSchemas`、cache 隔离；**新增 3 条回归**（#5 评审驱动）：小写 owner 写/读均归一（`introspectAndCache(owner="thbi")` → `schema_name="THBI"`，大写再内省复用同 row，无重复）、Oracle 空串 owner → 默认 `UPPER(username)` 键、PG 显式 owner → 键恒 `''`。
- 前端 vitest 受影响套件 **53 passed**（ImportWizard 19 / LocalImportInitPage 5 / datasourceApi 12 / localImportApi 2 / DatasourcePage 15）；`tsc -b` 0 错。**新增 2 条**：api 层 schema 参数契约 + owner 加载失败「下一步禁用 + 选择复位 + 重试恢复」。变更文件 funcs 覆盖率（scoped v8）：`datasource.ts 100 / ImportWizard 100 / SchemaStep 100`。

## 6. 代码评审（#5 驱动修复）

- **python-reviewer**：HIGH owner 未归一即作缓存键（小写/空白 → 重复行）→ 引入 `_normalizeSchemaName` 单一入口修复 + 3 回归；MEDIUM 非 Oracle 缓存到用户 owner 名下 / 空串 owner 落 `''` 键 → 同上修复；LOW 迁移回填加 TRIM。
- **typescript-reviewer**：MEDIUM owner 加载失败重试死路（同 owner 重选无事件 + 可进入空表步）→ `loadSchema` 返回成败 + 失败复位 `selectedSchema` + schema 步 Next 门控 `schemaReady` + 回归测试；LOW Spin tip 不可见 → 包空块展示。其余为不可达的潜在竞态（父级 `key` remount 已封口），记录不改。

## 7. 部署 + 运行时 E2E（真容器）

- 后端镜像 `qa-system-backend:patched3-20260909`（= `latest`）：`docker cp app/ + scripts/ + alembic/versions/`（含 0048）→ commit（ENTRYPOINT `["/bin/sh","-c"]`，runbook §15 可靠形式）→ `up -d`。启动自动 `alembic upgrade head` → prod `qa_metadata` 到 **0048**。
- **踩坑**：`CREATE TABLE AS schema_cache_bak_20260909`（迁移前安全备份）会触发 lifespan schema-drift 检查（「DB 存在但 ORM 未声明的表」）拒绝启动；已 pg_dump 到宿主 `/tmp/schema_cache_bak_20260909.sql` 后 DROP，重启即恢复。备份必须用 pg_dump/外部库，**不要**在库内建同库影子表。
- 前端 `qa-system-frontend` 离线 rebuild 成功（新 bundle `index-BecIJCH1.js`，内含 schema 步代码）。
- 运行时 E2E（curl 直打容器后端，stub auth）：
  - `GET /datasources/1/schemas` → 24 个真实 owner（`APPQOSSYS…SYS…THBI…X3…ZJTH…`）。
  - `POST /introspect?schema=THBI` → **82 张 THBI 表**（39 张 DW：`ADS_SUPPLIER_360 / DWD_* / DIM_*`…）；`GET /schema`（默认）→ 仍 **1676 张 ZJTH**（向后兼容）。
  - 缓存键归一：`GET /schema?schema=thbi`（小写）返回与 `THBI` **同一行**（同 `cachedAt`）；`schema_cache` 中 `(1,ZJTH)` 与 `(1,THBI)` 两行并存（此前单列唯一只允许一行）。
  - `POST /import-preview {"rules":{},"schema":"THBI"}` → 200；82 个 proposedClass 的 `sourceTable` **全部为裸表名**（0 个带 `THBI.` 前缀）。

## 8. 边界与注意事项

- `listSchemas` 会把系统 owner（`SYS/CTXSYS/AUDSYS`…）一并列出（连接用户可见对象的所有者）；向导内由用户显式挑选，不自动选首个（避免误选 SYS）。
- `BIN$…$0`（Oracle recyclebin）表出现在 THBI 清单里，owner 字段为空——由用户筛选/默认黑名单处理，非本特性引入。
- 全量前端 functions 80% 全局门槛仍被其他 0% funcs 文件预存拉低（见记忆 qa-system-frontend-coverage-gate），本特性变更文件 funcs 均已 ≥100%。

## 9. 待办

- git commit（工作树含 0048 迁移、SchemaStep.tsx、datasourceApi.test.ts 等新文件）——等待用户指示。
