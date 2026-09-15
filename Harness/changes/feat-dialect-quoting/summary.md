# 变更:5 个 evaluator SQL 标识符按 dialect 选引号(MySQL 反引号)

- **日期**:2026-09-15
- **作者**:Claude
- **Phase**:Phase 6 数据质量(评估器)
- **状态**:implemented(2026-09-15)
- **关联变更**:[[feat-eval-fail-reason]] 评估链路;[[silent-failure-hunter]] 复盘
- **迁移版本**:无(纯 SQL 拼接层改造)
- **MEMORY**:`../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/dialect-quoting-mysql-backticks.md`

## 根因

data_quality_evaluators 早期版本硬编码 `f'FROM "{table}"'` / `f'COUNT("{column}")'` /
`f'"{ref_table}"."{ref_column}"'`,对 PG/Oracle 合法,MySQL 数据源执行报
`pymysql 1064 syntax error`(双引号被当字符串字面量,不是标识符引号)。

dispatcher.collectSamples 的 blanket `except Exception: return []` 把整批采样吞掉,
13 条 MySQL 数据源 COMPLETENESS 规则全 ERROR 但页面无样本提示,**静默 3 个月**。

## 改法

1. **`backend/app/services/data_quality_evaluators/_common.py`**:新增两个 helper
   - `quote_identifier(adapter, name)` — Oracle/PG 双引号、MySQL 反引号、未知 adapter 回退 PG
   - `quote_qualified_name(adapter, *parts)` — 限定名每段独立 quote 后用 `.` 连接
   - `has_created_at_column` 的探测 SQL 也改用 quote_identifier(适配 MySQL)

2. **`backend/app/infrastructure/business_db_pool.py`**:`_SqlaAdapter.__init__`
   新增 `self.dialect = "postgresql" if url.startswith("postgresql") else "mysql"`

3. **5 个 evaluator**(`completeness.py` / `validity.py` / `uniqueness.py` /
   `consistency.py` / `referential.py`):所有硬编码双引号替换为 `quote_identifier` /
   `quote_qualified_name`;`referential.py` 同时改 EXISTS 子查询里的限定名

## 单测覆盖

`backend/app/tests/unit/test_data_quality_evaluators.py` 新增 `TestQuoteIdentifier` (5 case)
+ `TestCompleteness.test_mysql_adapter_uses_backticks`(回归保护)。3 个 fake adapter:
`_FakeOracleAdapter` / `_FakeMysqlAdapter` / `_FakePostgresAdapter` 均继承真实 adapter
类并 override `execute_read_only`(否则 `_ensureEngine()` 抛 AttributeError)。

## 部署

- 后端:`docker cp` 7 个文件 + `docker restart qa-backend`
- 前端:无改动
- DB 迁移:无

## 验证

```
uv run pytest app/tests/unit/test_data_quality_evaluators.py \
                  app/tests/unit/test_data_quality_evaluator_dispatcher.py \
                  app/tests/unit/test_data_quality_evaluator_time_window.py \
                  app/tests/unit/test_sample_limit_clause.py \
                  app/tests/unit/test_value_sampler.py
→ 92 passed in 15.49s
```

## 未来扩展 SQL Server / openGauss

在 `quote_identifier` 里加 isinstance 分支:SQL Server `[name]`、openGauss 兼容 PG 双引号。
