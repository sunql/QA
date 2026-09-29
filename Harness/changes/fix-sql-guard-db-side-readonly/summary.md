# 变更：fix-sql-guard-db-side-readonly

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：「库侧只读兜底」（chat-service-assessment §13 残差 #1，提案 `2026-09-26-sql-guard-db-side-readonly-proposal.md`）
- **状态**：done（应用层部分）；库侧授权由 DBA 执行（已交 `scripts/db-readonly-account-setup.sql`）
- **关联变更**：`fix-sql-guard-side-channel-and-reject-feedback`（M1/M2 解析层黑名单）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §13 残差 #1 + §2.3 M1 提案

---

## 1. 需求

SQL Guard 黑名单是**枚举式**防御——已实测抓到 `pg_sleep_for` / `pg_sleep_until` / `DBMS_LOB.LOADFROMFILE` / `MASTER_POS_WAIT` / `lo_put` / 引号包裹的 `"nextval"` 等漏项。解析层永远追不上方言扩展/自定义函数/未来新增函数。

任何「解析层漏过 → 直击业务库」都是**数据已出库**——不可逆损害。

本批目标：**给业务查询套上「事务级只读」结构级兜底**，与解析层构成纵深防御：

| 层 | 作用 | 失败后果 |
|---|---|---|
| **库侧只读（本批 + DBA）** | 真闸门：写/出网/文件读在数据库层根本不被允许 | 拦截失败 = 无后果（库拒绝） |
| **解析层黑名单（M1/M2 已修）** | UX 快速失败 + M2 自愈反馈（让 LLM 看到违规点） | 漏拦 = 退化为「库侧拒绝」 |
| **应用层只读事务（本批）** | 纵深：即便解析层漏过 + 库侧授权不到位，事务级只读也兜底 | 同上 |

## 2. 设计评审

### 候选方案

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 仅给 DBA 跑 SQL（不动应用代码） | 否决：库侧授权涉及外部系统，DBA 排期不可控；本仓库可独立做的部分不应等外部 |
| B | 仅改应用代码（不动库侧） | 否决：应用层 `SET TRANSACTION` 在会话级 / 服务端超时等场景下有边界，**不是真闸门** |
| C | **应用层 `SET TRANSACTION READ ONLY` + `ALTER SESSION SET READ ONLY` + DBA 三方言 SQL 清单**（选定） | 纵深防御；本仓库部分可独立提交 + 真实验证，DBA 部分给运维跑通 |

### 三方言应用层注入语法

| 方言 | 语法 | 关键约束 |
|---|---|---|
| **PostgreSQL** | `SET TRANSACTION READ ONLY`（事务级） | **必须在 `begin()` 之后**：SET TRANSACTION 会隐式提交当前事务；裸执行会落到独立小事务、立即 commit、丢失只读设置 |
| **MySQL 8.0+** | `SET SESSION TRANSACTION READ ONLY`（会话级） | 不需要 SUPER 权限 |
| **Oracle 12c+** | `ALTER SESSION SET READ ONLY`（会话级） | Oracle 没有事务级只读语法；旧版本 <12c 应用层**无解**，依赖库侧授权 |

### 检测方言

`_SqlaAdapter` 已存 `self.dialect: str`（`postgresql` / `mysql`，按 URL 前缀识别）⇒ 直接复用，无新增分支检测。

### 不做

- **不删 SQL Guard 黑名单**：作为快速失败 + M2 自愈反馈，仍有 UX 价值（让 LLM 看到违规点改写）。
- **不发明协议 / 不发明 SQL 变体**：三方言语法均来自官方文档，跨方言已确认无解的部分转给 DBA。
- **不动 Oracle 旧版本**：<12c Oracle 没有应用层只读语法，纯靠库侧授权；不发明变通。
- **不动连接池配置**：`_ensureEngine` 的 `pool_size=2, max_overflow=2` 与本批无关。

## 3. 数据模型变更

无。

## 4. 接口契约变更

| 面 | 变更 |
|---|---|
| `_SqlaAdapter.execute_read_only` | 内嵌 `async with conn.begin():`；`begin()` 内第一步 `SET TRANSACTION READ ONLY`（PG）或 `SET SESSION TRANSACTION READ ONLY`（MySQL），再原 SQL |
| `_OracleAdapter.execute_read_only` | `cursor.execute(sql)` 前加 `cursor.execute("ALTER SESSION SET READ ONLY")` |
| `BusinessDbAdapter` Protocol | 文档补充「执行前会在事务/会话级设置只读模式」（行为可见但接口签名不变） |
| `scripts/db-readonly-account-setup.sql`（新文件） | 三方言只读账号 SQL 清单（DBA 执行，不被应用 import） |

## 5. 实现要点

- **TDD**：先写 `test_business_db_pool_readonly.py`（4 例）：
  1. PG：`SET TRANSACTION READ ONLY` 在 `begin()` 后的同一只读事务中执行，原 SQL 紧随其后
  2. MySQL：`SET SESSION TRANSACTION READ ONLY`，**不**走 PG 语法
  3. Oracle：`ALTER SESSION SET READ ONLY`，**不**走 PG/MySQL 语法
  4. 只读注入不影响 `fetchmany(limit)` 行数限制
- **既有测试修复**：`test_datasource_pool.py` 的 `_FakeConn` 补 `begin()` + 4 例既有 Oracle/PG 断言补前置 `ALTER SESSION SET READ ONLY`（这是「写测试时未复核代码」的同类漂移，与 `agent-loop.md` 同根因）。
- **PG 关键约束**：必须在 `begin()` 之后执行 `SET TRANSACTION READ ONLY`；裸执行会隐式提交并丢失只读设置——这是 SQLAlchemy 异步路径最常见的踩坑点（来自探索阶段 `aa3669e9cc23984d4` 的实证）。
- **MySQL 选 `SET SESSION` 不用 `SET GLOBAL`**：`SET GLOBAL` 需要 SUPER 权限；`SET SESSION` 仅当前会话只读，不影响其他账号——多账号共存下唯一可行方案。
- **Oracle 不可伪造「事务级」**：Oracle 没有 `SET TRANSACTION READ ONLY` 语法，`ALTER SESSION SET READ ONLY` 是会话级且仅 12c+ 生效；老版本依赖 DBA 配置只读账号。

## 6. 测试

| 层 | 范围 | 结果 |
|---|---|---|
| **新增守卫** | `test_business_db_pool_readonly.py` 4 例 | **4 passed**（先 RED：3 例 SET/SELECT 顺序缺前置 → GREEN） |
| **既有单测** | `test_datasource_pool.py` 104 例 | **104 passed**（`_FakeConn.begin()` + 4 例 Oracle/PG 断言补前置 `ALTER SESSION SET READ ONLY`） |
| **回归切片** | `test_datasource_api.py` + `test_chat_stream_api.py` | **passed**（含 datasource API + 流式端点链路） |
| **ruff** | `business_db_pool.py` + 2 个 test 文件 | 6 行变更，ruff 绿 |
| **既有失败不扩大** | ADS 加权死代码 / Header.lower | memory 记录的预存失败未触碰 |

## 7. 安全审查

### 7.1 收益（结构级）

- **PG**：事务级只读，`INSERT/UPDATE/DELETE/CREATE` 在同一事务里被库引擎拒绝；不依赖解析层、不依赖应用代码、不依赖 DBA 排期。
- **MySQL 8.0+**：会话级只读事务；不依赖 SUPER 权限（多账号共存场景唯一可行）。
- **Oracle 12c+**：会话级只读；旧版本依赖 DBA 配置只读账号（脚本已就绪）。

### 7.2 边界与已知限制（如实标注）

| 边界 | 影响 |
|---|---|
| **PG 9.x 及以下 `SET TRANSACTION READ ONLY` 无效** | 项目 PG 版本 ≥ PG 10，实测有效 |
| **MySQL 5.7 不支持 `SET SESSION TRANSACTION READ ONLY`** | 库侧 `read_only` 全局变量兜底（脚本已就绪） |
| **Oracle <12c** | 应用层**无解**，仅靠库侧只读账号（DBA 脚本已就绪） |
| **事务边界外的写入** | 本批只覆盖 `execute_read_only`；其它写路径（管理后台 / alembic / seed 脚本）**不应**走只读事务 |
| **解析层黑名单未删** | 黑名单仍有 UX 价值（M2 自愈反馈让 LLM 改写）；保留 |

### 7.3 残差

- **库侧授权执行**：依赖 DBA 配合；脚本已在 `scripts/db-readonly-account-setup.sql` 给运维；不在本批 commit 范围内。
- **真机探针**：应用层注入正确性已在单测中用 `executed` 列表断言；DBA 跑通后建议用只读账号执行 `INSERT/UPDATE/DELETE` 验证库侧拒绝（本批不做）。

## 8. 部署验证（2026-09-27）

- `./scripts/deploy_backend.sh` 启动成功；`business_db_pool.py` + `test_datasource_pool.py` + `test_business_db_pool_readonly.py` md5 **MATCH**（3/3）。
- 健康：`/api/v1/health` 直连 8000 与经 nginx 5173 均 200。
- 无 alembic / 无前端改动 / 无镜像重建需求。

## 9. 关联

- commit：
  - `feat: SQL Guard 库侧只读兜底（PG/MySQL 事务级 + Oracle 会话级只读 SQL 注入）`
  - `test: 业务库只读事务守卫（PG/MySQL/Oracle 三方言 + 行数限制兼容）`
  - `fix: test_datasource_pool 既有断言补前置只读注入（_FakeConn.begin + Oracle 例）`
  - `docs: DBA 三方言只读账号配置清单（SQL 脚本）`
- 提案落地：`Harness/changes/2026-09-26-sql-guard-db-side-readonly-proposal.md` §3「应用层兜底」部分已实现；§3「库侧只读账号」转给 DBA 执行（脚本就绪）。
- 评估文档：`Harness/wiki/chat-service-assessment.md` §13 残差 #1 标注「应用层已修；DBA 部分等运维执行」。
- memory：登记 `qa-system-sql-guard-db-side-readonly`，包含「PG `SET TRANSACTION` 必须在 `begin()` 之后」「MySQL 选 `SET SESSION` 不选 `SET GLOBAL`」「Oracle 应用层无解的版本边界」「既有测试断言漂移与 agent-loop.md 同根因」。
- 关联条目：
  - `qa-system-sql-guard-side-channel`（M1/M2 解析层黑名单）
  - `qa-system-doc-drift-cleanup`（同类漂移：`agent-loop.md` 虚构实现）

## SSOT 校验清单

- [x] PG `_SqlaAdapter.execute_read_only` 内 `async with conn.begin():` 包裹原 SQL；第一步 `SET TRANSACTION READ ONLY`
- [x] MySQL 走 `SET SESSION TRANSACTION READ ONLY`（检测 `self.dialect == "postgresql"`）
- [x] Oracle 走 `ALTER SESSION SET READ ONLY`
- [x] 三方言各 1 个单测通过（含执行顺序断言）
- [x] 既有 `_FakeConn.begin()` 补齐 + 4 例 Oracle/PG 断言同步
- [x] `test_datasource_pool.py` 104/104 通过；`test_business_db_pool_readonly.py` 4/4 通过
- [x] DBA 脚本三方言就绪（PG/MySQL/Oracle，含敏感函数撤销 + Resource Manager）
- [x] ruff 绿；无 alembic / 无前端改动
- [x] 容器 md5 3/3 MATCH + health 200×2