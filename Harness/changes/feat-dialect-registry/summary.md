# 变更：feat-dialect-registry

- **日期**：2026-10-08
- **作者**：Claude
- **Phase**：Phase X.Y dialect-registry refactor
- **状态**：done
- **关联变更**：[feat-dialect-guard-research](../feat-dialect-guard-research/summary.md)（前序：SQL Guard 拦截层）
- **迁移版本**：无（纯 refactor，ORM schema 不动）
- **MEMORY**：[../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-dialect-registry.md](../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-dialect-registry.md)

---

## 1. 需求

研究会话（research session）在 Oracle 19c 上 8/8 finding 失败，根因是 LLM 写出 MySQL 语法（DATE_FORMAT / DATE_SUB / INTERVAL）→ ORA-00907 → 0 行 → 报告 data 章节无 chart 块。前序 commit 0d3b752 + b9c8aa7 在 `hypothesis_verificationSql` 与 `runVerification` 两条路径加了 **拦截式 SQL Guard**，但用户指出这是补丁思路——正确的做法是从 chat 继承方言体系，让 research 像 chat 一样按数据源类型+版本选用对应方言，遇到不确定项与用户多轮确认形成完整报告。

**目标**：把 `nl2sql_dialects.py` 从「Oracle-only if 分支 + 拦截式 Guard」重构为**显式多版本方言注册表**：
- (DataSourceType, version) → SqlDialect 映射，版本字符串规范化（19c/21c/9i/10g/11g/8.0/5.7/14/15/16 等各自有专属方言）
- 守卫从"模块级 dict + if-else"改为 SqlDialect 实例方法 `check_sql()`，banned set / keyword patterns / interval 守卫成为方言属性
- 现有 chat 调用方（`Nl2SqlService.resolveDialect` / `checkSqlDialect`）**零修改**——薄壳化后内部委托给 registry

**验收**：
- Oracle 11g/12c/19c/21c/9i/10g 全部返回正确方言
- MySQL 8.0/5.7 各自有专属 notes
- PostgreSQL 14/15/16 各自有专属 notes
- 既有 chat prompt 拼装行为不变（sampleLimitSql / limitRule / dateArithmeticRule / identifierRule 全部保留）
- 检查 SQL 时 hint 文案保留 Oracle 专属建议（"用 TO_CHAR..."），不退回通用提示

## 2. 设计评审

**候选方案 A1**：在 `_SQL_DIALECTS` dict 上加版本 key（如 `(DataSourceType.ORACLE, "19c")` → dialect）——扩展现有数据结构。
- 取舍：改动小但 dict 形状被打破，类型提示需重写；公共 API 表面（`_SQL_DIALECTS[...]`）也得改。
- 否决：dict 不利于注册流程的"按 db 列举 + 显式 withEntry"心智，且外部 monkeypatch 测试需要更复杂 setup。

**候选方案 A2**：抽 `DialectRegistry` 类，frozen dataclass + `MappingProxyType` 不可变，`withEntry(dbType, bucket, dialect)` 返回新实例，`resolve(dbType, version)` 走 fallback 链。
- 取舍：API 显式（`withEntry` 而非 dict 赋值），不变量集中（重复注册抛 ValueError / frozen 不允许 mutate），新 dialect 添加只需 `withEntry`。
- 选定：与 chat 端"按 db 列举"心智一致；frozen + 不可变映射防止运行时漂移。

**最终决定**：A2 + SqlDialect 扩字段（bannedFunctions / keywordPatterns / intervalAllowed / notes / datasourceType）。

**多视角意见**：
- 代码评审：frozen + MappingProxyType 是必要约束（防止 chat 端在运行时 monkeypatch dialect）
- 兼容：既有 `checkSqlDialect(sql, dsType)` 与 `resolveDialect(dsType, version)` 公共签名零修改，薄壳化后内部委托给 registry
- 安全：守卫仍覆盖 Oracle/MySQL/PG 全部 dialect 路径，研究侧 `runVerification` 与 `hypothesis_verificationSql` 两条路径都受同一 registry 治理

## 3. 数据模型变更

无。纯内存重构，不动 ORM schema / alembic 迁移 / 本体节点。

## 4. 接口契约变更

模块内（`app/services/nl2sql_dialects.py`）：

- 新增 `SqlDialect.check_sql(sql) -> DialectGuardResult` 实例方法
- 新增 `SqlDialect.bannedFunctions: tuple[str, ...]`、`keywordPatterns: tuple[tuple[str, str, str], ...]`、`intervalAllowed: bool`、`notes: str`、`datasourceType: DataSourceType | None` 字段（全部默认空/True/None，向后兼容）
- 新增 `DialectRegistry` 类：`@dataclass(frozen=True)` + `MappingProxyType` 不可变 entries；`withEntry(dbType, bucket, dialect) -> DialectRegistry` 抛 ValueError on duplicate；`resolve(dbType, version) -> SqlDialect` 走 candidate bucket 链 fallback
- 新增模块级 `_BUILTIN_DIALECT_REGISTRY = _build_default_registry()`（12 条内置方言：Oracle default/11g/19c/21c；MySQL default/8_0/5_7；PostgreSQL default/14/15/16）

外部：
- `checkSqlDialect(sql, dsType)` 签名零变，行为不变（含 Oracle 专属 hint 提示）
- `resolveDialect(dsType, oracle_version)` 签名零变，未知类型回退 Oracle 11g 语义保留
- 行为变化点：Oracle 19c 现在解析为 `_SQL_DIALECTS_ORACLE_19C`（专属 notes）而非 12c；这是新能力（refactor 前 19c 走 12c），相应测试 `test_resolveDialect_Oracle19c_应走_12c_分支` 改为语义断言（FETCH FIRST + 19c notes），`is` identity 改为 sampleLimitSql 内容校验

## 5. 实现要点

关键文件：
- `app/services/nl2sql_dialects.py`（+~150 行）
  - `SqlDialect` 扩 5 字段 + `check_sql()` 实例方法
  - 新增 `DialectRegistry` frozen dataclass
  - 新增 `_candidateBuckets(db, version)` 按 db 类型生成 fallback 链
  - 12 条内置方言通过 `dataclasses.replace()` 克隆既有 11g/12c/MySQL/PG 实例后注入 banned set / notes
  - `checkSqlDialect` 薄壳化（直接委托给 `dialect.check_sql(sql)`）
  - `resolveDialect` 薄壳化（直接委托给 `DialectRegistry.default().resolve(...)`）
- `app/services/nl2sql_service.py`：`_SQL_DIALECTS` 导入删除（不再需要）
- `app/tests/prompts/test_dialect_check_sql.py`（新增，9 测试）— SqlDialect.check_sql 实例方法单测
- `app/tests/prompts/test_dialect_registry.py`（新增，17 测试）— DialectRegistry + 12 条内置方言 + fallback 链
- `app/tests/prompts/test_nl2sql_dialects_backcompat.py`（新增，10 测试）— refactor 前后行为不变性钉死（9i/10g 走 11g、19c/21c 走专属、None 走 default）
- `app/tests/prompts/test_nl2sql_dialects.py`（修改）— 2 个 `is` identity 测试改 sampleLimitSql 语义断言；2 处 `_SQL_DIALECTS[POSTGRESQL/MYSQL]` 改 `DialectRegistry.default().resolve(...)`
- `app/tests/prompts/test_nl2sql_date_arithmetic_prompt.py`（修改）— 4 处 `_SQL_DIALECTS[...]` 改 `DialectRegistry.default().resolve(...)`
- `tests/unit/test_nl2sql_prompt_template.py`（legacy，pytest 范围外）— 修复 `_SQL_DIALECTS` 导入断链

关键算法：
- `_candidateBuckets`：
  - Oracle：`version.startswith(("9", "10", "11")) or "11g" in v` → `["11g"]`；否则按命名版本（19c/21c/12c）→ `["19c", "default"]` 等
  - MySQL：`8.0.46` → `["8_0", "8", "default"]`
  - PostgreSQL：`16.4` → `["16", "default"]`
- `DialectRegistry.resolve` 走 candidate 链：exact → major → "default" → KeyError

依赖注入点：
- `DialectRegistry.default()` 返回 module-level singleton `_BUILTIN_DIALECT_REGISTRY`
- 业务调用方通过 `resolveDialect(...)` / `checkSqlDialect(...)` 间接使用，无需注入

## 6. 测试

新增 / 改动测试文件：
- `app/tests/prompts/test_dialect_check_sql.py` — 9 测试（empty SQL passes、banned function 大小写不敏感、keyword pattern、interval 守卫、向后兼容老构造）
- `app/tests/prompts/test_dialect_registry.py` — 17 测试（withEntry 重复抛 ValueError、string→enum 大小写不敏感、12 条内置方言各自命中、Oracle 9i/10g 走 11g、fallback 链、frozen 不允许 mutate）
- `app/tests/prompts/test_nl2sql_dialects_backcompat.py` — 10 测试（refactor 前后行为不变性钉死）

测试结果（执行 `cd backend && pytest app/tests/prompts/`）：
- **63 passed**（baseline 23 + test_nl2sql_date_arithmetic_prompt 4 + test_dialect_check_sql 9 + test_dialect_registry 17 + test_nl2sql_dialects_backcompat 10）

集成测试（`test_research_sql_runner.py`）：
- **11 passed**（含 `test_run_verification_dialect_guard_rejects_mysql_in_oracle` 回归守卫）

集成测试（`test_research_agent_service.py`）：
- **49 passed / 3 failed**（3 红 = 已知 baseline：MAX_REPLAN_PER_TURN NameError，与本 refactor 无关）

单元测试（`test_nl2sql_service.py`）：
- **29/29 dialect 相关 passed**（含 case-insensitive string 解析；该问题在 registry resolve 中通过 `dbType.lower()` 修复）

## 7. 安全审查

触发条件：SQL Guard 用户输入校验 / LLM 输出反向校验。

- 触发了 `security-reviewer`（内部）：本次 refactor 是把拦截式 guard 升级为治理式方言注册，**没有放宽**任何安全检查。
- CRITICAL：无
- HIGH：无
- MEDIUM：无
- LOW：Oracle 19c 现在解析为专属方言（refactor 前走 12c）。这是新能力不是回归，且 `test_resolveDialect_Oracle19c_应走_12c_分支` 已改为语义断言钉死新行为（FETCH FIRST + 19c notes）。

## 8. 部署验证

纯后端 refactor，ORM schema 不动、容器镜像不需要重 build。部署步骤：
1. `git pull` 拉到本 refactor 的 commit
2. `docker compose restart backend` 即可生效（容器内 Python 进程加载新代码）
3. 冒烟：调用 `POST /api/v1/chat` 触发 NL2SQL，确认 system prompt 仍含 `Oracle 数据库` / `FETCH FIRST` 关键词

真实数据验证：
- `pytest app/tests/integration/test_research_sql_runner.py -v` 11/11 passed（守卫在真实 PG/Neo4j fixture 上仍生效）
- `pytest app/tests/integration/test_research_agent_service.py -v` 49/52 passed（3 红 = pre-existing baseline）

## 9. 关联

- 设计稿：[../../docs/superpowers/specs/2026-10-08-dialect-registry-refactor-design.md](../../docs/superpowers/specs/2026-10-08-dialect-registry-refactor-design.md)
- 计划：[../../docs/superpowers/plans/2026-10-08-dialect-registry-refactor-plan.md](../../docs/superpowers/plans/2026-10-08-dialect-registry-refactor-plan.md)
- 前序 commit：0d3b752（research session SQL guard for hypothesis verificationSql）+ b9c8aa7（dialect guard in runVerification with retry-with-feedback）
- Memory：[../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-dialect-registry.md](../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-dialect-registry.md)
- 关联变更：未来 B（研究继承 chat 的知识体系 / 本体）+ C（研究多轮知识缺口确认），本 refactor 是 A

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 2 个候选方案对比 + 选定理由
- [x] 第 3 段无迁移（纯 refactor）
- [x] 第 7 段安全审查 LOW 一项（新能力说明）
- [x] 第 8 段冒烟命令 + 测试结果
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档已更新（本 summary.md 自身即是文档落点）
- [x] MEMORY 索引添加在 `MEMORY.md`
- [x] 涉及真实 SQL/DB 改动时集成测试已跑通（test_research_sql_runner 11/11）
