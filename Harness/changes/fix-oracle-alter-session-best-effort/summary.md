# 变更：fix-oracle-alter-session-best-effort

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：Oracle 库侧只读兜底
- **状态**：done（**两阶段：best-effort 降级 + 真实路径切换**）
- **关联变更**：`fix-sql-guard-db-side-readonly`（27a2299/1304f6c，上批引入 Oracle `ALTER SESSION SET READ ONLY`）
- **迁移版本**：无
- **SSOT 出处**：故障报告「该步骤执行失败：首次：ORA-02248: 无效的 ALTER SESSION 选项；重试后仍执行失败：ORA-02248」（生产对话链路全断）

---

## 1. 需求

### 生产故障

- 现象：任何走 Oracle 数据源的对话步骤均失败；首次 ORA-02248 后重试仍 ORA-02248
- 错误：`ORA-02248: 无效的 ALTER SESSION 选项`
- 影响：业务查询全部走不通，链路彻底瘫痪
- 根因：上批「库侧只读兜底」在 Oracle 路径注入 `ALTER SESSION SET READ ONLY`，但 THBI 19c 实例拒收该选项

### 根因深度诊断（2026-09-27 真机探针）

```
=== Test 1: ALTER SESSION SET NLS_LANGUAGE ===
OK - basic ALTER SESSION works          ← 用户有 ALTER SESSION 权限 ✓
=== Test 2: ALTER SESSION SET READ ONLY ===
FAIL: ORA-02248: invalid option for ALTER SESSION   ← 但 READ ONLY 选项被拒 ✗
=== Test 3: SET TRANSACTION READ ONLY ===
OK - SET TRANSACTION READ ONLY works    ← 事务级可用 ✓
```

**根因更正**：**不是用户缺权限**（基础 ALTER SESSION 证明有权限）；**不是 Oracle 版本**（19c 文档支持该语法）；**是该 19c 实例（THBI 192.168.205.70:1521/X3V71ORA）的某些配置（受限 PDB / OCM / 受限 instance）拒绝 `ALTER SESSION SET READ ONLY` 这一特定选项**。

### 修复目标

- **阶段 1（commit 06940b5）**：`ALTER SESSION SET READ ONLY` 包 try/except → warning → 继续，best-effort 不阻塞业务
- **阶段 2（本批后续 commit）**：真机探针发现 `SET TRANSACTION READ ONLY` 在该实例可用，**改用它**做事务级只读（单 cursor.execute 后 SELECT 自动落在 readonly tx 内）

纵深防御三层（任一即可兜底）：
- 层 1：SQL Guard 解析层黑名单（M1/M2）
- 层 2：DBA 维护的只读账号 + 撤销敏感权限
- 层 3：SET TRANSACTION READ ONLY（本批最终方案）

---

## 2. 设计评审

### 方案选择

| 候选 | 选定 | 理由 |
|---|---|---|
| A. 删除 Oracle 库侧只读注入 | ❌ | 牺牲层 3 防御 |
| B. 启动时探测 Oracle 版本/选项，按版本分支 | ❌ | 探测 SQL 本身可能 ORA |
| **C. 改用 `SET TRANSACTION READ ONLY`（事务级）** | ✅ | 真机确认该实例可用；对**单语句 SELECT** 等价于会话级 readonly |
| D. 强制要求 DBA 修复实例配置 | ❌ | 实例配置不可控；DBA 也未必能改（受限于 CMAN/CDB） |

### 不做

- **不发明 Oracle 选项探测**：`ALTER SESSION SET READ ONLY` 是否被拒必须真机跑，探测 SQL 不能预测结果
- **不动 chat_service 重试**：ORA-02248 是基础设施问题，不是用户/数据问题
- **不强制 DBA 修实例配置**：用户「已授予 ALTER SESSION 权限」仍被拒为「invalid option」，属于实例配置层，DBA 也可能无法改

---

## 3. 数据模型变更

无。

---

## 4. 接口契约变更

无（仅 `_OracleAdapter.execute_read_only` 内部行为变更）。

---

## 5. 实现要点

`business_db_pool.py:570-580`（当前版本）：Oracle 路径改用 `SET TRANSACTION READ ONLY`：

```python
try:
    await cursor.execute("SET TRANSACTION READ ONLY")
except Exception as tx_exc:  # noqa: BLE001 - best-effort 降级路径
    logger.warning(
        "Oracle 事务级只读注入失败 reason=%s exc=%s "
        "降级到 SQL Guard + 只读账号双层防线，原 SQL 继续执行",
        "SET_TRANSACTION_REJECTED",
        tx_exc,
    )
await cursor.execute(sql)
```

历史版本（commit 06940b5）：`ALTER SESSION SET READ ONLY` 包 try/except。

---

## 6. 测试

| 层 | 范围 | 结果 |
|---|---|---|
| **新增守卫** | `test_business_db_pool_readonly.py::test_oracle_set_transaction_failure_does_not_block_query` | 1 passed |
| **回归** | `test_business_db_pool_readonly.py` 4 例原有用例（含 `test_oracle_execute_read_only_sets_transaction_readonly` 改写） | 4 passed |
| **真机探针** | `scripts/probe_oracle_readonly_privilege.py` | 4/5 PASS（§4 SELECT ANY TABLE 缺，但 owner 级别 grant 足够 §5 业务 SELECT） |
| **端到端真机** | `docker exec qa-backend` 直接调用 execute_read_only | OK rows=[{'x': 1}]，无 ORA-02248 |
| **ruff** | 2 文件 | 1 项预存 SIM117（与本批无关） |

### 新增测试断言要点（`test_oracle_set_transaction_failure_does_not_block_query`）

- 模拟 `cursor.execute("SET TRANSACTION READ ONLY")` 抛 ORA
- 断言 1：原 SQL `SELECT 1 FROM dual` 仍执行
- 断言 2：原 SQL 返回行（`rows == [{"dummy": 1}]`，来自 fetchmany mock）
- 隐含断言：执行未抛错 → `_run()` 正常返回 → 业务链路继续

---

## 7. 安全审查

### 收益（安全性）

- 纵深防御恢复完整：解析层黑名单 + 只读账号 + SET TRANSACTION 三层都在
- 第三层（SET TRANSACTION）成功时，写操作被库拒绝（真机验证 INSERT → ORA-01031）
- 第三层失败时，记 warning 不抛错——运维/审计仍能看见降级事件
- 业务 SQL 继续执行，链路恢复

### 边界与已知限制

- **不变量**：Oracle 实例即使拒绝 SET TRANSACTION READ ONLY（理论上的 <12c 旧版本 / 受限 PDB），仍受 (1) SQL Guard 黑名单 + (2) DBA 维护的只读账号双层保护
- **运维可见性**：warning 日志含 `reason=SET_TRANSACTION_REJECTED` + exc 内容，可聚合失败率
- **会话级只读**：本批改用事务级，对**单 cursor.execute 的 SELECT** 等价；如未来需要多语句 readonly session，需另寻方案（可能是 ora-access-mode 连接参数）

### 残差

- **多语句 readonly session**：当前实现每次只跑一条 SELECT，无此场景需求；预留挂账
- **若 THBI 实例恢复 ALTER SESSION 选项**：可双轨保留（先尝试 SET TRANSACTION，备 ALTER SESSION）—— 本批不做（YAGNI）

---

## 8. 部署验证（2026-09-27）

- `./scripts/deploy_backend.sh` ✓（多次执行）
- 容器 md5 比对 `business_db_pool.py` + `test_business_db_pool_readonly.py`
- 真机探针：THBI 实例下 SET TRANSACTION READ ONLY 通过 + SELECT 1 返回
- `/api/v1/health` 双通道 200
- 对话链路恢复：Oracle 数据源 SQL 查询不再因 ORA-02248 阻塞
- `docker exec qa-backend` 直接调用 execute_read_only：OK rows=[{'x': 1}]

### DBA 真机探针脚本（2026-09-27 补充）

为验证生产只读账号的 Oracle 权限是否齐全（层 3 防御能否生效），新增：

- `backend/scripts/probe_oracle_readonly_privilege.py`：6 段式探针（§1 连接 / §2 版本 / §3 ALTER SESSION 权限 / §4 SELECT ANY TABLE / §5 业务 SELECT / §6 写入拒绝）
- 跑法：
  ```bash
  ORACLE_DSN=<host:port/service_name> \
  ORACLE_USER=<qa_readonly> \
  ORACLE_PASSWORD=<strong_random> \
  python scripts/probe_oracle_readonly_privilege.py
  ```
- §3 失败 = ORA-02248 风险存在；§6 失败 = 账号根本不是只读账号（CRITICAL，禁止用于业务链路）
- 真机结果（THBI 19c）：§1/§2/§3/§5/§6 PASS，§4 FAIL（THBI 缺 SELECT ANY TABLE 但有 owner 级 grant，业务 SELECT 仍 OK）

---

## 9. 关联

- **历史 commit**：
  - `06940b5`：ALTER SESSION SET READ ONLY 包 try/except → warning 继续（阶段 1 best-effort 降级）
  - `a0ed7d9`：probe_oracle_readonly_privilege.py（DBA 真机探针脚本）
  - **当前待提交**：`SET TRANSACTION READ ONLY` 替换 ALTER SESSION（阶段 2 真机路径切换）
- **上批 commit** `27a2299/1304f6c`：库侧只读兜底初版（Oracle 路径过于严格，本批修）
- memory 既有 `qa-system-sql-guard-db-side-readonly` 预言「Oracle <12c 应用层无解」——预言错方向
- memory 新登记：`qa-system-oracle-alter-session-best-effort`（含本批深度诊断 + 教训）

---

## SSOT 校验清单

- [x] `business_db_pool.py:_OracleAdapter.execute_read_only` Oracle 路径用 `SET TRANSACTION READ ONLY`
- [x] 失败时仅记 warning，不抛错
- [x] 原 SQL 继续执行
- [x] 新增测试 `test_oracle_set_transaction_failure_does_not_block_query` 通过
- [x] `test_oracle_execute_read_only_sets_transaction_readonly` 改写通过
- [x] 4 例原有用例回归通过
- [x] 真机探针（THBI 19c）确认 SET TRANSACTION READ ONLY 可用
- [x] 容器部署验证 OK，无 ORA-02248
- [x] ruff delta = +0（预存 SIM117 与本批无关）
- [x] 评估文档 / memory 同步（含根因更正）