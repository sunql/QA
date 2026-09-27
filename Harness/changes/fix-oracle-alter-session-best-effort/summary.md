# 变更：fix-oracle-alter-session-best-effort

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：Oracle 库侧只读兜底 best-effort 降级（生产故障修复）
- **状态**：done
- **关联变更**：`fix-sql-guard-db-side-readonly`（27a2299/1304f6c，上批引入 Oracle `ALTER SESSION SET READ ONLY`）
- **迁移版本**：无
- **SSOT 出处**：故障报告「该步骤执行失败：首次：ORA-02248: 无效的 ALTER SESSION 选项；重试后仍执行失败：ORA-02248」（生产对话链路全断）

---

## 1. 需求

### 生产故障

- 现象：任何走 Oracle 数据源的对话步骤均失败；首次 ORA-02248 后重试仍 ORA-02248
- 错误：`ORA-02248: 无效的 ALTER SESSION 选项`
- 影响：业务查询全部走不通，链路彻底瘫痪
- 根因：上批「库侧只读兜底」在 Oracle 路径注入 `ALTER SESSION SET READ ONLY`，但部署实例拒绝该语法
  - 候选根因：Oracle <12c（无此语法）、12c+ 但用户缺 `ALTER SESSION` 权限、PDB 受限
  - Memory 预存：「Oracle <12c 应用层无解，依赖库侧授权」（已正确预言此风险）

### 修复目标

`ALTER SESSION SET READ ONLY` 是 best-effort 纵深防御的第三层，不应在实例拒绝时阻塞业务查询：
- 解析层黑名单（M1/M2）—— 第一层防线，仍在
- DBA 维护的只读账号 + 撤销敏感权限（`scripts/db-readonly-account-setup.sql`）—— 第二层防线，仍在
- ALTER SESSION 会话级只读（本次）—— 第三层，**best-effort**

第三层失败时，必须降级到前两层，绝不阻塞业务 SQL 执行。

---

## 2. 设计评审

### 方案选择

| 候选 | 选定 | 理由 |
|---|---|---|
| A. 删除 Oracle ALTER SESSION 注入 | ❌ | 12c+ 实例仍可受益；不应一刀切 |
| B. 启动时探测 Oracle 版本，按版本分支 | ❌ | 探测需要发版本查询 SQL，连接失败/权限不足时探测本身就可能 ORA；徒增复杂度 |
| **C. ALTER SESSION 包 try/except，失败记 warning 继续执行** | ✅ | 与 M1/M2 解析层「失败 reason= 上报」的处置一致；不动其他防御层 |
| D. 在 chat_service 重试循环加 ORA-02248 特判 | ❌ | 把 Oracle-specific 错误冒泡到上游，污染业务编排层 |

### 不做

- **不删 Oracle 路径的 ALTER SESSION 注入**：12c+ 仍能用，保留纵深防御
- **不发明 Oracle 版本探测**：探测失败同样是 ORA，无法可靠分支
- **不动 chat_service 重试**：ORA-02248 是基础设施问题，不是用户/数据问题

---

## 3. 数据模型变更

无。

---

## 4. 接口契约变更

无（仅 `_OracleAdapter.execute_read_only` 内部行为变更）。

---

## 5. 实现要点

`business_db_pool.py:570-578` 把 `ALTER SESSION` 包在 try/except 内：

```python
try:
    await cursor.execute("ALTER SESSION SET READ ONLY")
except Exception as alter_exc:  # noqa: BLE001 - best-effort 降级路径
    logger.warning(
        "Oracle 会话级只读注入失败 reason=%s exc=%s "
        "降级到 SQL Guard + 只读账号双层防线，原 SQL 继续执行",
        "ALTER_SESSION_REJECTED",
        alter_exc,
    )
await cursor.execute(sql)
```

不抛错 → 不被 `asyncio.wait_for` 识别为超时 → 不被外层 retry 误判为可重试 → 业务 SQL 继续执行。

---

## 6. 测试

| 层 | 范围 | 结果 |
|---|---|---|
| **新增守卫** | `test_business_db_pool_readonly.py::test_oracle_alter_session_failure_does_not_block_query` | 1 passed |
| **回归** | `test_business_db_pool_readonly.py` 4 例原有用例 | 4 passed |
| **ruff** | 2 文件 | 1 项预存 SIM117（与本批无关） |

### 新增测试断言要点

- 模拟 `cursor.execute("ALTER SESSION SET READ ONLY")` 抛 `ORA-02248`
- 断言 1：原 SQL `SELECT 1 FROM dual` 仍执行（`executed == ["SELECT 1 FROM dual"]`）
- 断言 2：原 SQL 返回行（`rows == [{"dummy": 1}]`，来自 fetchmany mock）
- 隐含断言：执行未抛错 ⇒ `_run()` 正常返回 ⇒ 业务链路继续

---

## 7. 安全审查

### 收益（安全性）

- 纵深防御不变：解析层黑名单 + 只读账号 + ALTER SESSION 三层都在
- 第三层（ALTER SESSION）失败时，记 warning 不抛错——运维/审计仍能看见降级事件
- 业务 SQL 继续执行，链路恢复

### 边界与已知限制

- **不变量**：Oracle 实例即使拒绝 ALTER SESSION，仍受 (1) SQL Guard 黑名单（M1/M2）+ (2) DBA 维护的只读账号双层保护
- **运维可见性**：warning 日志含 `reason=ALTER_SESSION_REJECTED` + exc 内容，可聚合失败率
- **生产环境探测**：建议后续 (a) 确认 Oracle 版本是否 <12c，(b) 若 ≥12c 则补 `ALTER SESSION` 系统权限给只读账号；本批**不**做这两项（属于运维/DBA 任务）

### 残差

- **「应用层无解」场景（Oracle <12c）永久降级到双层防御**：本批接受此 gap，与 memory 「依赖库侧授权」一致
- **DBA 任务：补 `ALTER SESSION` 权限或确认版本**：登记待 DBA 执行，不在本批

---

## 8. 部署验证（2026-09-27）

- `./scripts/deploy_backend.sh` ✓
- 容器 md5 比对 `business_db_pool.py` + `test_business_db_pool_readonly.py`
- 真机探针：模拟 ORA-02248 后原 SQL 仍返回正确行
- `/api/v1/health` 双通道 200
- 对话链路恢复：Oracle 数据源 SQL 查询不再因 ORA-02248 阻塞

---

## 9. 关联

- commit（待提交）：
  - `fix: Oracle ALTER SESSION 失败降级为 warning（best-effort 三层防御保持业务不断）`
  - `test: ORA-02248 降级路径守卫（mock raise → 原 SQL 仍执行 + 返回行）`
- 上批 commit `27a2299/1304f6c`：库侧只读兜底初版（Oracle 路径过于严格，本批修）
- memory 既有 `qa-system-sql-guard-db-side-readonly` 已正确预言「Oracle <12c 应用层无解」；本批落实 best-effort 降级
- memory 新登记：`qa-system-oracle-alter-session-best-effort`

---

## SSOT 校验清单

- [x] `business_db_pool.py:_OracleAdapter.execute_read_only` ALTER SESSION 包 try/except
- [x] 失败时仅记 warning，不抛错
- [x] 原 SQL 继续执行
- [x] 新增测试 `test_oracle_alter_session_failure_does_not_block_query` 通过
- [x] 4 例原有用例回归通过
- [x] ruff delta = +0（预存 SIM117 与本批无关）
- [x] 评估文档 / memory 同步