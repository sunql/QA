# 变更：NL2SQL 多方言（#66）

- **日期**：2026-08-12
- **作者**：AI 助手
- **Phase**：Phase 3 多数据源 + NL2SQL（多方言缺口补齐）
- **状态**：done

## 1. 需求

NL2SQL 生成器当前把方言与 schema 前缀写死为 Oracle / `ZJTH.`，多数据源下会误导 LLM 生成错误 SQL：

1. **方言不随数据源变化**：System Prompt 固定要求 "Oracle 数据库" 与 "FETCH FIRST N ROWS ONLY"，MySQL/PostgreSQL 数据源也会生成 Oracle 语法。
2. **schema 前缀硬编码 `ZJTH.`**：表名前缀与 JOIN 示例写死 `ZJTH.`，与数据源实际 schema 归属无关。

验收标准：
- `datasource.type=mysql/postgresql` 时，System Prompt 注入对应方言规则（`LIMIT N`），不注入 Oracle 的 `FETCH FIRST N ROWS ONLY`。
- 表名前缀提示与 JOIN 示例使用 `datasource.username`（真实 Oracle demo username 恰为 `ZJTH`，行为与旧硬编码一致）。

## 2. 设计评审

- **方言注册表（关键决策）**：`SqlDialect` frozen dataclass（`name` + `limitRule` 完整句子）+ `_SQL_DIALECTS` 按 `DataSourceType` 索引（Oracle/MySQL/PostgreSQL）。rule 3 由 `dialect.limitRule` 插值，Oracle 保持原文本。
- **resolveDialect 回退（关键决策）**：`datasourceType=None` 或未知字符串（DB 手改脏值）回退 Oracle 并告警，避免单条脏数据令整个 NL2SQL 失败；`DataSourceType(datasourceType)` 同时兼容枚举与字符串。
- **schema 前缀来自 username**：`generateSql` 以 `schemaPrefix` 参数接收 `ds.username`；rule 5 前缀提示与 rule 7 JOIN 示例统一用它（无前缀时用裸表名）。真实 demo 数据源 username=ZJTH，验证 JOIN 示例与旧输出逐字一致。
- **KISS**：仅改 prompt 构造与两个调用点，不新增 API/表；`oracleSchema` 参数重命名为 `schemaPrefix`（无外部调用方依赖）。

## 3. 数据模型变更

无。复用现有 `data_source.type`（`DataSourceType` 枚举）与 `data_source.username`。

## 4. 接口契约变更

无 API 契约变化。`Nl2SqlService.generateSql` 内部签名：`oracleSchema` → `datasourceType` + `schemaPrefix`（均为可选，向后兼容省略调用）。

## 5. 实现要点

- `app/services/nl2sql_service.py`：新增 `SqlDialect`（`name`/`limitRule`/`joinTemplate`/`useSchemaPrefix`）/ `_SQL_DIALECTS` / `resolveDialect`（大小写不敏感匹配 + 未知回退 Oracle）；`_buildSystemPrompt(schemaText, dialect, schemaPrefix, context)` 注入方言名、行数限制规则、schema 前缀提示（仅 Oracle）与 JOIN 示例。
- `app/services/chat_service.py`：`processMessage` 与 `_streamNl2Sql` 两处调用点传 `datasourceType=ds.type, schemaPrefix=ds.username`；`_streamNl2Sql` 新增 `ds: DataSource` 参数（由 `_streamQuery` 传入）。

## 6. 测试

- `test_nl2sql_service.py` 新增 `TestSqlDialect`（10 例）：缺省回退 Oracle、MySQL 用 LIMIT、PostgreSQL 用 LIMIT、schema 前缀流入提示与 JOIN 示例、无前缀用裸表名、`resolveDialect` 枚举/字符串/未知类型回退、大小写不敏感字符串、ZJTH 用户名金丝雀、MySQL 通用示例且不限定前缀。
- `test_chat_service.py` 新增 3 例：默认数据源（ORACLE/username=u）方言与前缀流入真实 NL2SQL prompt、MySQL 走 LIMIT 且无前缀、PostgreSQL 走 LIMIT 且无前缀。
- `test_chat_service_stream.py` 新增 1 例（流式路径同断言）。
- 全量：**286 passed**，覆盖率 **88.46%**（≥80% 门槛）。

## 7. 安全审查

`python-reviewer` 首轮 0 CRITICAL / 0 HIGH，4 个 MEDIUM + LOW-1 全部修复并通过复核（lint/mypy 干净，代码可合并）。修复明细：

| 级别 | 问题 | 修复 |
|------|------|------|
| MEDIUM | Oracle 的 ERP 表名 JOIN 示例泄漏到 MySQL/PG prompt | JOIN 示例移入 `SqlDialect`，MySQL/PG 用通用示例表 `sales`/`customers` |
| MEDIUM | MySQL/PG 用 username 作 schema 前缀语义错误（`root.表名`） | `useSchemaPrefix` 仅 Oracle 为真；MySQL/PG 不注入前缀提示、表名不限前缀 |
| MEDIUM | 编排层缺 PostgreSQL 方言测试 | `test_chat_service.py` 新增 PG 数据源用例 |
| MEDIUM | `resolveDialect("MySQL")` 大小写输入误回退 Oracle | 增加大小写不敏感字符串匹配，匹配失败才回退 Oracle |

顺带修复 LOW-1（规则 5 恒存在，消除 4→6 编号断档）。

## 8. 部署验证

- 真实 PG 读取数据源 id=2（ZJTH-Oracle，type=oracle，username=ZJTH）+ 19 个本体类，走真实 `generateSql`（捕获 prompt 的假 LLM）：
  - prompt 含 `Oracle 数据库`、`FETCH FIRST N ROWS ONLY`、`ZJTH.表名` 前缀提示、`FROM ZJTH.PRECEIPTD d JOIN ZJTH.PRECEIPT h` 示例、`不要使用 LIMIT` —— 与旧硬编码行为逐字一致。
- 同一脚本验证 MySQL（username=root）与 PostgreSQL（"POSTGRESQL" 大写字符串）：
  - prompt 含对应方言名与 `LIMIT`；不含 `FETCH FIRST N ROWS ONLY`、不含 `表名使用 schema 前缀`、无 `root.`/`postgres.` 前缀；JOIN 示例为通用 `FROM sales s JOIN customers c`。（提示中出现的 `PRECEIPTD` 来自真实本体 schema 列表——该表确实存在，非示例注入。）
- MySQL/PostgreSQL 真实业务库不在本环境，方言规则以单元测试覆盖。

## 9. 关联

- 设计稿：`docs/设计02-详细设计.md`（Phase 3 多数据源 + NL2SQL）
- Wiki：`Harness/wiki/nl2sql-engine.md`、`Harness/wiki/data-model.md`
- 前置：`Harness/changes/feat-phase3-datasource/`
