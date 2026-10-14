"""ResearchSqlRunner 单元测试：常量与契约冒烟（不触达真实 PG）。

行为级集成测试在 ``tests/integration/test_research_sql_runner.py``（真实 PG + 真实 adapter）。

本任务范围（hardcode-cleanup plan Task 1.3）：验证 ``LOG_SQL_TRUNCATE_LEN`` 命名常量存在
且等于 120，与 ``research_sql_runner.py:94`` 行上限截断日志一致。
"""

from __future__ import annotations


def test_log_sql_truncate_len() -> None:
    """验证查询超行截断日志里 SQL 字符串的字符上限应走命名常量 ``LOG_SQL_TRUNCATE_LEN=120``。

    防止运维日志里 ``sql[:120]`` 改单位后下游日志解析失配（截断长度无 SSOT）。
    """

    from app.services import research_sql_runner

    assert research_sql_runner.LOG_SQL_TRUNCATE_LEN == 120
    assert isinstance(research_sql_runner.LOG_SQL_TRUNCATE_LEN, int)