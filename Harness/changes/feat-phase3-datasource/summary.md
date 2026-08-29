# 变更：feat-phase3-datasource

- **日期**：2026-08-12
- **Phase**：Phase 3 - 多数据源 + 单表 NL2SQL（数据源管理先行）
- **状态**：done

## 1. 需求
实现业务数据源的注册、连接测试、CRUD 管理，为 NL2SQL 提供可配置多数据源基础。
验收标准：
- 可 CRUD 数据源，密码 Fernet 加密存储且读 DTO 永不返回。
- 支持 Oracle / PostgreSQL / MySQL 三类业务库，按 `datasource_id` 缓存连接适配器。
- 连接参数在 `DATASOURCE_HOST_ALLOWLIST` 白名单内（配置了才限制）。
- 只读 SQL Guard：仅允许 SELECT/WITH，拒绝 DML/DDL/DCL/多语句，行数限制 + 超时。
- 默认数据源唯一（is_default 排他），删除默认源后自动提升首个启用源。
- 前端数据源管理页（表格 + 新增/编辑弹窗 + 弹窗内连接测试）。

## 2. 设计评审
- 密码存储：复用 `infrastructure/security/crypto.py` Fernet 工具（`encryptApiKey`），SECRET_KEY 须为合法 Fernet 密钥。
- Oracle 接入：SQLAlchemy 无官方异步方言，用 `python-oracledb 4.x` 原生异步 API（`connect_async`/`AsyncConnection`/`AsyncCursor`）包装为统一 `BusinessDbAdapter` 协议；PG/MySQL 走 SQLAlchemy 异步引擎（asyncpg / aiomysql）。
- 连接参数以独立字段持久化（host/port/database_name/username/encrypted_password），前端表单直观，后端按类型动态构造连接串。
- 只读校验用 `sqlparse.parse` 白名单（SELECT/WITH），拒绝 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/GRANT/MERGE/CALL/EXEC 等及多语句脚本。
- 前端 `POST /datasources/test` 返回 `{ success, message }`，其 success 字段与统一信封冲突，故用独立原始 axios 实例调用，避免拦截器吞掉失败 message。

## 3. 数据模型变更
- 新增表 `data_source`（迁移 `alembic/versions/0003_datasource_table.py`，down 指向 `0002_ontology_tables`）：
  - `id`（BigIntPk 自增）、`name`（唯一）、`type`（mysql/postgresql/oracle）、`host`、`port`、`database_name`、`username`、`password_encrypted`、`description`、`is_active`、`is_default`、`created_by`、`created_time`/`updated_time`（TimestampMixin）。
  - 约束：`uq_datasource_name` 唯一约束，索引 `idx_datasource_name`、`idx_datasource_default`。

## 4. 接口契约
- `GET /api/v1/datasources`（`activeOnly` 查询参数）→ `list[DataSourceRead]`
- `POST /api/v1/datasources` → 201 `DataSourceRead`
- `GET /api/v1/datasources/{datasourceId}` → `DataSourceRead | 404`
- `PUT /api/v1/datasources/{datasourceId}` → `DataSourceRead | 404 | 422`
- `DELETE /api/v1/datasources/{datasourceId}` → 204 | 404
- `POST /api/v1/datasources/test` → `DataSourceTestResponse`
- JSON 输出 camelCase；`DataSourceRead`/响应不含 `password` 与 `passwordEncrypted`。
- 错误沿用统一包络 `{ success: false, error, detail }`（NotFound→404 / ValidationError→422 / 其他→400）。

## 5. 实现要点
- `infrastructure/business_db_pool.py`：`_SqlaAdapter`（PG/MySQL）、`_OracleAdapter`（oracledb 原生 async）、`build_adapter`/`get_adapter`（按 datasource_id 缓存）/`dispose_adapter`/`reset_pool`、`_assert_read_only`、URL 密码百分号编码。
- `services/datasource_service.py`：create/list/get/update/delete/test_connection；主机白名单、名称唯一、默认排他 + 提升、连接参数变更时释放缓存适配器。
- `api/v1/datasource.py`：路由挂载 `prefix="/api/v1/datasources"`（继续绕过 FastAPI 0.141 `_IncludedRouter` prefix 叠加 bug），`main.py` 与测试 `conftest.py` 同步。
- `pyproject.toml`：新增依赖 `aiomysql>=0.2.0`。
- 前端：`types/datasource.ts`、`api/datasource.ts`（`testDataSource` 用 rawClient）、`pages/DatasourcePage.tsx`（替换空壳：表格 + 弹窗表单 + 弹窗内测试连接 + 类型切换自动填充默认端口）。

## 6. 测试
- 后端：110 个测试通过，覆盖率 82.98%（≥80%）。
  - `unit/test_datasource_pool.py`：只读校验白名单/黑名单/多语句/空、URL 构造与密码编码、适配器类型、缓存/释放、Oracle execute 路径（mock 连接）。
  - `integration/test_datasource_api.py`：CRUD 契约、camelCase、密码不回显、重复名 422、主机白名单 422、默认排他性、删除提升默认、连接测试端点。
- 前端：60 个测试通过（8 个文件），`tsc -b` 通过。
  - `tests/DatasourcePage.test.tsx`：渲染列表、新增提交、编辑不传空密码、删除确认、弹窗内测试连接（成功/未填密码提示）。
  - `tests/pages.test.tsx`：DatasourcePage 断言更新为管理页，mock datasource API。
- 说明：前端项目暂无 eslint 配置文件（`lint` 脚本为遗留声明），类型以 `tsc` 为准。

## 7. 安全审查
- 密码 Fernet 加密存储，读 DTO 永不返回（`DataSourceRead` 无密码字段）。
- 主机白名单 `DATASOURCE_HOST_ALLOWLIST`（配置了才限制，空则放行）。
- SQL Guard：仅允许 SELECT/WITH 单语句；`sqlparse` 校验 + `queryRowLimit` 行数限制 + `queryTimeoutSeconds` 超时。
- 前端密码输入 `Input.Password`，编辑时留空表示不修改；连接测试走独立 axios 实例以保留后端错误 message。

## 8. 部署验证（真实环境冒烟）
- `alembic upgrade head` 成功应用到本地 PostgreSQL 16（`data_source` 表 13 列 + 唯一约束 + 索引就绪）。
- 直连 Oracle 适配器冒烟：`192.168.205.70:1521/X3V71ORA`（ZJTH）连接成功，`SELECT COUNT(*) FROM ZJTH.PRECEIPT` 返回 970,304 行。
- ASGI 全栈冒烟（真实 Postgres + 真实 Oracle）：
  - 创建 201，响应无密码、`is_default=true`、`created_by=anonymous` ✅
  - `POST /datasources/test` 200 `{success: true, message: "连接成功"}` ✅
  - 列表、删除 204、删除后列表为空 ✅
- 冒烟中发现并修复的问题：
  1. **oracledb 4.x `AsyncCursor.close()` 为同步方法**（返回 None，非协程），`await cursor.close()` 抛 `TypeError: 'NoneType' object can't be awaited`；改为同步调用并新增 mock 回归测试。`AsyncConnection.close()`/`ping()` 为协程，不受影响。

## 9. 关联
- 设计稿：`docs/设计01.md`、`docs/设计02.md`
- Wiki：`Harness/wiki/api-reference.md`、`data-model.md`
- 规则：`Harness/rules/权限与安全规范.md`、`数据与AI治理规范.md`
