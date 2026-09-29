"""sample_limit_clause 单测（feat-dq-evaluation-report 回归）。

复盘：早期版本固定写 `` LIMIT N``，Oracle 报 ``ORA-00933: SQL 命令未正确结束``，
dispatcher 又有 blanket ``except Exception: return []`` 把整批采样吞掉，报告里
「规则全 FAIL 但无一条违规样本」静默 N 个月。

本测试锁住两点：
1. Oracle adapter → `` AND ROWNUM <= N``，与已有 WHERE 子句拼成合法 SQL。
2. 其它 adapter（PostgreSQL/SQL Server/MySQL）→ `` LIMIT N``，行为不变。
3. limit <= 0 → 空串（不限）。

不在这里测真实业务库；只验 SQL 子句形状。
"""

from __future__ import annotations

import pytest

from app.infrastructure.business_db_pool import _OracleAdapter
from app.services.data_quality_evaluators._common import sample_limit_clause


class _FakePgAdapter:
    """非 Oracle adapter（类名 != _OracleAdapter 即可）。"""


class _FakeSqlServerAdapter:
    pass


class _FakeMySqlAdapter:
    pass


def test_oracle_returns_rownum_clause() -> None:
    adapter = _OracleAdapter.__new__(_OracleAdapter)  # 绕过 __init__，不连库
    clause = sample_limit_clause(adapter, 20)
    assert clause == " AND ROWNUM <= 20"
    # 关键：必须 AND 进去（5 个 sampler 主体 SQL 都已带 WHERE）
    assert clause.startswith(" AND ")


def test_postgres_returns_limit_clause() -> None:
    clause = sample_limit_clause(_FakePgAdapter(), 20)
    assert clause == " LIMIT 20"


def test_sqlserver_returns_limit_clause() -> None:
    clause = sample_limit_clause(_FakeSqlServerAdapter(), 20)
    assert clause == " LIMIT 20"


def test_mysql_returns_limit_clause() -> None:
    clause = sample_limit_clause(_FakeMySqlAdapter(), 20)
    assert clause == " LIMIT 20"


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_non_positive_limit_returns_empty(limit: int) -> None:
    assert sample_limit_clause(_FakePgAdapter(), limit) == ""
    assert sample_limit_clause(
        _OracleAdapter.__new__(_OracleAdapter), limit,
    ) == ""


def test_oracle_rownum_is_integer_safe() -> None:
    """limit 必须是整数；防御性转换。"""
    adapter = _OracleAdapter.__new__(_OracleAdapter)
    # float 入参应该被截断成 int（避免 ORA-01795 之类）
    clause = sample_limit_clause(adapter, 20.7)
    assert clause == " AND ROWNUM <= 20"
