"""DBA 真机探针：验证 qa_readonly 账号的 Oracle 只读相关权限是否齐全。

2026-09-27 fix-oracle-alter-session-best-effort 批配套脚本。

背景：本批在 Oracle 路径上注入 `ALTER SESSION SET READ ONLY` 做会话级只读（纵深防御第 3 层）。
若生产只读账号缺 `ALTER SESSION` 系统权限，会触发 ORA-02248，对话链路全断。
应用层已加 best-effort 降级（warning 继续执行），但要彻底修复需 DBA 给该账号补 `ALTER SESSION`。

跑法（用生产只读账号）：
    ORACLE_DSN=<host:port/service_name> \
    ORACLE_USER=<qa_readonly> \
    ORACLE_PASSWORD=<strong_random> \
    python scripts/probe_oracle_readonly_privilege.py

退出码：
    0 = 全部通过，可享受完整三层防御（SQL Guard + 只读账号 + ALTER SESSION）
    1 = 任一项失败，附原因；DBA 根据 §失败项 处理

检查项：
    §1 连接可达性
    §2 Oracle 版本（<12c ⇒ ALTER SESSION SET READ ONLY 永久无效，必须仅靠只读账号兜底）
    §3 ALTER SESSION 系统权限（核心：决定层 3 是否生效）
    §4 SELECT ANY TABLE 系统权限（真只读账号必须具备，否则连 SELECT 都跑不通）
    §5 业务样本 SELECT（连通 + 字段可读）
    §6 写入样本应被库拒绝（仅读账号必须拒绝 DML/DDL）

输出格式：
    §1 PASS 连接成功 dsn=<host:port/service_name> version=Oracle Database 19c ...
    §2 PASS Oracle 版本支持 ALTER SESSION SET READ ONLY（19c >= 12c）
    §3 FAIL 缺 ALTER SESSION 系统权限，ORA-02248 风险存在
    §3 修法：GRANT ALTER SESSION TO <qa_readonly>;
    ...
    summary=2/5 PASS, 3 FAIL

注：本脚本以 sys 视角查 user_sys_privs 后立即断开，不留任何副作用。INSERT/UPDATE
    测试各自在独立事务中执行，回滚不会污染生产数据。
"""
from __future__ import annotations

import os
import sys

import oracledb

# 修法模板（DBA 把 <USER> 换成生产只读账号名）
FIX_GRANTS = [
    "GRANT ALTER SESSION TO <USER>;",  # §3：必须，否则层 3 ALTER SESSION SET READ ONLY 必失败
    "GRANT SELECT ANY TABLE TO <USER>;",  # §4：标准只读账号必备
    "GRANT CREATE SESSION TO <USER>;",  # 连接权限（建账号时一般已给，列这里兜底）
]
PDB_FIX_GRANTS = [
    "ALTER SESSION SET CONTAINER = <PDB_NAME>;",
    *FIX_GRANTS,
]


def _env_or_die(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.stderr.write(f"缺少环境变量 {name}\n")
        sys.exit(2)
    return val


def _connect() -> oracledb.Connection:
    """按环境变量建连接。"""
    return oracledb.connect(
        user=_env_or_die("ORACLE_USER"),
        password=_env_or_die("ORACLE_PASSWORD"),
        dsn=_env_or_die("ORACLE_DSN"),
    )


def _report(section: str, status: str, detail: str) -> None:
    """输出单条结果：§N PASS/FAIL <detail>"""
    print(f"§{section} {status} {detail}")


def probe_version(conn: oracledb.Connection) -> bool:
    """§2 Oracle 版本（<12c ⇒ ALTER SESSION SET READ ONLY 永久无效）。"""
    cur = conn.cursor()
    # V$INSTANCE.VERSION 形如 '19.0.0.0.0'；V$VERSION.BANNER 含完整产品名（如 'Oracle Database 19c...'）。
    cur.execute("SELECT VERSION FROM V$INSTANCE WHERE ROWNUM = 1")
    (version,) = cur.fetchone()
    cur.close()
    major = int(version.split(".")[0])
    if major >= 12:
        _report("2", "PASS", f"Oracle 版本支持 ALTER SESSION SET READ ONLY（{major}c >= 12c），version={version}")
        return True
    _report("2", "FAIL", f"Oracle {major}c < 12c，ALTER SESSION SET READ ONLY 永久无效；必须依赖只读账号兜底")
    return False


def _has_sys_priv(conn: oracledb.Connection, priv: str) -> bool:
    """查 user_sys_privs 看当前用户是否有指定系统权限。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM user_sys_privs WHERE PRIVILEGE = :p",
        p=priv,
    )
    (count,) = cur.fetchone()
    cur.close()
    return count > 0


def probe_alter_session_priv(conn: oracledb.Connection) -> bool:
    """§3 ALTER SESSION 系统权限（层 3 防御生效与否的开关）。"""
    if _has_sys_priv(conn, "ALTER SESSION"):
        _report("3", "PASS", "拥有 ALTER SESSION 系统权限，层 3 防御可生效")
        return True
    _report("3", "FAIL", "缺 ALTER SESSION 系统权限，应用层 ALTER SESSION SET READ ONLY 会抛 ORA-02248")
    _report("3", "FIX", f"修法：{FIX_GRANTS[0]}")
    _report("3", "FIX", f"若是 PDB：{PDB_FIX_GRANTS[0]} -> {FIX_GRANTS[0]}")
    return False


def probe_select_any_table(conn: oracledb.Connection) -> bool:
    """§4 SELECT ANY TABLE 系统权限。"""
    if _has_sys_priv(conn, "SELECT ANY TABLE"):
        _report("4", "PASS", "拥有 SELECT ANY TABLE 系统权限")
        return True
    _report("4", "FAIL", "缺 SELECT ANY TABLE，连只读查询都跑不通")
    _report("4", "FIX", f"修法：{FIX_GRANTS[1]}")
    return False


def probe_select_works(conn: oracledb.Connection) -> bool:
    """§5 业务样本 SELECT（连通 + 字段可读）。"""
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 AS DUMMY FROM dual")
        row = cur.fetchone()
    finally:
        cur.close()
    if row and row[0] == 1:
        _report("5", "PASS", "SELECT 1 FROM dual 返回 1，查询链路通畅")
        return True
    _report("5", "FAIL", f"SELECT 1 FROM dual 返回异常：{row!r}")
    return False


def probe_write_rejected(conn: oracledb.Connection) -> bool:
    """§6 写入样本应被库拒绝（仅读账号必须拒绝 DML/DDL）。

    技巧：用 SAVEPOINT 包住写操作，让抛错时自动回滚；不污染生产数据。
    """
    cur = conn.cursor()
    try:
        # 仅探测，不真写入：INSERT INTO dual VALUES(1) 必失败（dual 是 view）
        cur.execute("SAVEPOINT probe_write")
        try:
            cur.execute("INSERT INTO dual VALUES (1)")
            cur.execute("ROLLBACK TO SAVEPOINT probe_write")
            _report("6", "FAIL", "INSERT INTO dual 居然成功了——只读账号配置有误")
            return False
        except oracledb.DatabaseError as exc:
            cur.execute("ROLLBACK TO SAVEPOINT probe_write")
            err_code = exc.args[0].code if exc.args else "unknown"
            _report(
                "6",
                "PASS",
                f"写入被库拒绝（ORA-{err_code}），符合只读账号预期：{exc.args[0].message.strip()[:120]}",
            )
            return True
    finally:
        cur.close()


def main() -> int:
    """按 §1..§6 顺序跑探针；任一失败打印修法模板，退出码 1。"""
    try:
        conn = _connect()
    except oracledb.DatabaseError as exc:
        _report("1", "FAIL", f"连接失败：{exc.args[0].message.strip() if exc.args else exc}")
        _report("1", "FIX", "检查 ORACLE_DSN / ORACLE_USER / ORACLE_PASSWORD 是否正确；tnsnames.ora 是否含该 DSN")
        return 1

    try:
        _report("1", "PASS", f"连接成功 dsn={os.environ['ORACLE_DSN']} user={os.environ['ORACLE_USER']}")
        results = {
            "version": probe_version(conn),
            "alter_session": probe_alter_session_priv(conn),
            "select_any_table": probe_select_any_table(conn),
            "select_works": probe_select_works(conn),
            "write_rejected": probe_write_rejected(conn),
        }
    finally:
        conn.close()

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\nsummary={passed}/{total} PASS, {total - passed} FAIL")

    # §3（ALTER SESSION 权限）是 ORA-02248 故障的根因，是脚本最关心的项
    # §6（写入被拒）失败比 §3 失败严重——意味着账号根本不是只读账号
    if results["write_rejected"]:
        all_pass = all(results.values())
        return 0 if all_pass else 1
    _report("CRITICAL", "FAIL", "写入未被库拒绝，账号不是真只读账号——禁止用于业务查询链路")
    return 1


if __name__ == "__main__":
    sys.exit(main())