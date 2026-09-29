# 提案：SQL Guard 的库侧只读兜底（只读账号 + 服务端超时）

- **日期**：2026-09-26
- **状态**：proposal（未排期）
- **来源**：`fix-sql-guard-side-channel-and-reject-feedback` 批次 security-reviewer 的 MEDIUM-6（该批已把解析层黑名单补到「四大类侧信道 + 引号/注释/包名形态」，但仍属**枚举式**防御）
- **关联**：[fix-sql-guard-side-channel-and-reject-feedback](fix-sql-guard-side-channel-and-reject-feedback/summary.md)、`Harness/wiki/nl2sql-engine.md`（SQL Guard 段）、根 `CLAUDE.md` 核心约束 #2

## 问题

当前「业务查询只允许只读」的**唯一执行点**是解析层黑名单（`app/infrastructure/business_db_pool.py` 的 `_assert_read_only` / `_assertNoHiddenWrites`）。实测证据：

```
grep -rn "statement_timeout\|READ ONLY\|readOnly" backend/app/infrastructure/business_db_pool.py backend/app/config.py
→ 零命中
```

- `_SqlaAdapter.execute_read_only` / `_OracleAdapter.execute_read_only` 用 `get_adapter` 解密出的**完整业务凭据**执行，未 `SET TRANSACTION READ ONLY`、未使用只读角色、未设服务端语句超时；
- 唯一的超时是客户端 `asyncio.wait_for(..., queryTimeoutSeconds=30)`（`app/config.py`）——**客户端超时不能阻止服务端继续执行**（特别是 Oracle 侧取消依赖驱动行为）；
- 顺带纠正文档漂移：`Harness/wiki/nl2sql-engine.md` 原写「连接级 `statement_timeout`」，实际不存在（本批已在 wiki 中改正）。

## 为什么这不是「再加几个黑名单」能解决的

黑名单是**枚举**：每补一批就暴露下一批（本批实测即抓到 `pg_sleep_for`/`pg_sleep_until`、`DBMS_LOB.LOADFROMFILE`、`MASTER_POS_WAIT`、`lo_put`、引号包裹的 `"nextval"` 等）。解析层**永远追不上**：方言扩展函数、用户自定义函数、未来版本新增函数都在枚举之外。

因此需要分层，而不是把黑名单做厚：

| 层 | 作用 | 失败后果 |
|---|---|---|
| 库侧只读（本提案） | **真闸门**：写/出网/文件读在数据库层根本不被允许 | 拦截失败 = 无后果（库拒绝） |
| 解析层黑名单（已实现） | **UX 快速失败**：在发库前给出「哪里违规」的可自愈反馈（M2），省一次往返 | 漏拦 = 退化为「库侧拒绝 + 报错回灌」 |

## 建议方案（按方言）

1. **PostgreSQL**：为业务查询单独建**只读角色**（`GRANT SELECT` + `ALTER ROLE … SET default_transaction_read_only = on`），并设 `ALTER ROLE … SET statement_timeout = '30s'`；撤销 `EXECUTE` on `pg_read_file`/`pg_read_binary_file`/`pg_ls_dir`/`lo_*`/`dblink_*`/`pg_sleep*` 等（`PUBLIC` 默认权限需逐项 `REVOKE`）。
2. **Oracle**：使用只读用户或收回 `UTL_*` / `DBMS_*` 包的执行权限；设 `ALTER PROFILE … LIMIT CPU_PER_CALL / IDLE_TIME`（`MAX_EXECUTION_TIME` 需 Resource Manager）。
3. **MySQL**：只读账号（`GRANT SELECT`）+ `MAX_EXECUTION_TIME`（优化器 hint 或 `SELECT /*+ MAX_EXECUTION_TIME(30000) */`）或资源组。
4. **适配器层**：`execute_read_only` 开事务时显式 `SET TRANSACTION READ ONLY`（PG/MySQL 均支持），作为配置缺位时的第二道兜底。

## 风险与取舍

- **需要业务库侧的授权/配置变更**，涉及外部系统（Oracle THBI 等），不在本仓库控制范围内 ⇒ 需与 DBA 协作、按库分批推进；
- 只读角色可能**改变现有查询行为**（例如依赖临时表、序列的函数、`SET` 会话态），需先在测试库跑通全量 smoke；
- 客户端超时应保留（它管「不让用户等」），服务端超时管「不让库被拖死」，两者不互斥。

## 验收标准（落地时）

1. 三个方言各有一个只读账号，用该账号执行 `INSERT`/`UPDATE`/`pg_sleep(5)`/`SELECT … INTO` 全部被**库**拒绝（不依赖解析层）；
2. 长查询在服务端超时上限内被终止（用 `pg_sleep(60)` 或等价的笛卡尔积查询验证，且**连接被释放**）；
3. 既有业务查询 smoke 全绿（尤其依赖函数/会话态的查询）；
4. 解析层黑名单**保留**（作为快速失败与可自愈反馈），文档明确两者分工。
