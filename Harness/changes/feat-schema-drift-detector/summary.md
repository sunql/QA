# 变更：Schema 漂移检测与启动阻断

- **日期**：2026-08-30
- **作者**：AI 助手
- **状态**：done
- **触发事故**：线上查询抛 `asyncpg.exceptions.UndefinedTableError: relation "entity_mapping" does not exist`

## 1. 需求

ORM `Base.metadata` 与 DB `pg_tables` 在生产环境发生漂移，应用启动后第一个查询即失败。当前 lifespan 只建引擎，未校验 DB schema 与 ORM 一致性；本地 `uvicorn` 启动绕过 docker entrypoint 的 `alembic upgrade head && uvicorn` 链。

需要：
- 启动期 fail-fast：检测到漂移直接阻断，不让进程暴露给请求
- 工具化：可独立运行的脚本，本地 + CI 都能调用
- 紧急回滚兜底：环境变量 `SKIP_SCHEMA_CHECK=1` 可临时跳过

## 2. 设计评审

- **Alembic vs ORM 双轨检查**：
  - Alembic head（最新迁移 revision）vs DB `alembic_version` 当前值 → 捕获「差一个 migration」
  - ORM `Base.metadata.tables` vs DB `pg_tables` → 捕获「改了模型忘了写迁移」或「手动 DROP 表」
- **纯只读**：脚本不动 DB（只 SELECT `pg_tables` / `alembic_version`），与「SQL 业务库只读护栏」一致
- **同步 wrapper 备而不用**：`_checkDrift` 是异步函数的 sync 包装，给 alembic env 等无 running loop 场景备用
- **返回码分级**：
  - 0 = 无漂移（DB 已 upgrade head 或全新空库）
  - 1 = 漂移（缺表 / 版本滞后 / 多余表）
  - 2 = 环境错误（DATABASE_URL 未指向 PG / 连接失败）
- **blocking vs warning 分级**：ORM 缺表 + Alembic 滞后 = blocking（启动阻断）；ORM 多余表 = warning（默认不阻断，可 `--strict` 让 CI 零容忍）

## 3. 实现要点

- **`backend/scripts/check_schema_drift.py`** (261 行)：
  - `_parseRevisionFiles` 解析 `alembic/versions/*.py`，支持 `revision: str = "..."` 与 `revision = "..."` 两种风格
  - `_findHead` 找无任何 down_revision 指向的 revision；多/无 head 抛 `RuntimeError`
  - `_queryCurrentRevision` 查询 alembic_version；表不存在返回 None
  - `_queryDbTables` 列 public schema 表（排除 alembic_version）
  - `_queryOrmTables` 延迟导入 `Base`（避免 alembic env 副作用）
  - `_checkDriftAsync` 编排三个 check，返回 `list[str]` 漂移描述
- **`backend/app/main.py`** (lifespan)：默认开启 drift check，`SKIP_SCHEMA_CHECK=1` 跳过；漂移阻断时 `raise RuntimeError` 让进程退出
- **`Harness/rules/工程结构.md`**：新增「Schema 漂移防护（强制 CRITICAL）」段，含问题/要求/为什么/调试/CI 门禁/变更流程

## 4. 测试

- `backend/app/tests/integration/test_check_schema_drift.py` (9 用例)：
  - `_parseRevisionFiles` 现代 / 旧式 / 混合三种风格（3 用例）
  - `_findHead` 单链 / 多 head 抛错 / 循环抛错（3 用例）
  - `_checkDrift` subprocess 集成：healthy DB → exit 0；rename 表触发漂移 → exit 1 + "缺失"（2 用例）
  - `SKIP_SCHEMA_CHECK` 环境变量字符串存在性（1 用例）

## 5. 真实数据验证

- 真实 PG 5433 集成：healthy DB 检测 → exit 0
- 真实 PG 5433 集成：临时 RENAME 表 → 脚本 exit 1 + 输出 `[orm] ORM 声明但 DB 缺失的表：entity_mapping`
- 真实 PG 5433 集成：临时 RENAME 后 RENAME 回来 → 后续脚本运行 exit 0（确认隔离正确）

## 6. 部署与验证

```bash
cd backend
DATABASE_URL=... .venv/bin/python scripts/check_schema_drift.py --database-url $DATABASE_URL
# 期望：exit 0；输出 "Schema drift 检查通过：ORM 与 DB 完全一致。"

# 模拟漂移：rename entity_mapping → entity_mapping_drift_test
# 期望：exit 1；输出 "发现 1 项 schema 漂移：[orm] ORM 声明但 DB 缺失的表：entity_mapping"
```

## 7. 审查

- **Code Reviewer**：APPROVE；0 CRITICAL / 0 HIGH；1 MEDIUM info（`entity_mapping` 硬编码，未来表移除会让测试失败 — 接受为维护提示）；1 LOW info（env-var 字符串检查，非行为检查）
- **Security Reviewer**：APPROVE；0 issues；SQL 全为 `text(...)` 硬编码，无 f-string 注入；密码在日志中被 `split("@")` 屏蔽

## 8. 决策与遗留

**决策**：
1. 漂移检测放 lifespan（而非路由中间件）— fail-fast 比延迟到查询触发更安全
2. `SKIP_SCHEMA_CHECK` 文档化为「仅紧急回滚」— 写进 PR 描述与 summary 要求
3. 双轨（Alembic + ORM）— Alembic 版本对齐捕获「差 migration」，ORM 对齐捕获「改模型忘迁移」

**遗留**：
- 多余表默认不阻断（避免历史残留误伤）；CI 可加 `--strict` 零容忍
- 检测粒度为「表」，未做「列」粒度校验；下一步可考虑加入 INFORMATION_SCHEMA.COLUMNS 对比