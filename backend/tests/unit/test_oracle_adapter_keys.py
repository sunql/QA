"""Oracle 适配器返回的行字典键必须小写（与 SQLAlchemy 适配器一致）。

回归：供应商规则在 Oracle 数据源下 100% 评估失败，但 Oracle 实际有数据。
根因：oracledb 的 cursor.description 返回的是 Oracle 标识符字面大小写（默认大写），
evaluator 内部 `row.get("total")` / `row.get("passed")` 都是小写 key，命中 None
回退到 0，于是 total=0/passed=0/passRate=0/FAIL。

SQLAlchemy 适配器走 `result.mappings()`，行为本身就是小写；统一成小写即可让
5 个 evaluator（completeness/uniqueness/consistency/validity/referential）无需
逐个适配。
"""

from __future__ import annotations

import asyncio
from typing import Any


class _FakeDescription:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getitem__(self, idx: int) -> Any:
        return (self._name,)


class _FakeCursor:
    """模拟 oracledb 异步游标：列名按 Oracle 习惯大写返回。"""

    def __init__(self, rows: list[tuple[Any, ...]], columns: list[str]) -> None:
        self._rows = rows
        self._columns = columns
        # cursor.description 在 oracledb 里是 [(name, ...), ...] 的 list
        self.description = [_FakeDescription(c) for c in columns]

    async def execute(self, sql: str) -> None:
        return None

    async def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def close(self) -> None:
        return None


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    async def close(self) -> None:
        return None


class _FakeOracleModule:
    """替换 oracledb：返回一个把列名大写返回的 cursor，模拟真实行为。"""

    @staticmethod
    async def connect_async(*, user: str, password: str, dsn: str) -> _FakeConn:
        cursor = _FakeCursor(
            rows=[(3500, 3500)],
            columns=["TOTAL", "PASSED"],  # Oracle 实际返回大写
        )
        return _FakeConn(cursor)


def test_oracle_adapter_returns_lowercase_keys() -> None:
    """Oracle 适配器必须把列名规范成小写，与 SQLAlchemy 适配器一致。"""
    # import 在函数里，避免模块加载阶段把整个 app 拉进来（pytest 直接调用本测试时
    # 不依赖 conftest，绕过重量级 fixture）。
    from app.infrastructure import business_db_pool as bdp

    # 把 oracledb 模块属性换成假实现。
    bdp.oracledb = _FakeOracleModule  # type: ignore[attr-defined]

    adapter = bdp._OracleAdapter(  # noqa: SLF001 - 单元测试直构
        host="db", port=1521, service_name="ORCL",
        username="u", password="p",
    )
    rows = asyncio.run(adapter.execute_read_only(
        'SELECT COUNT(*) AS total, COUNT("X") AS passed FROM "T"'
    ))

    assert rows == [{"total": 3500, "passed": 3500}], (
        "Oracle 适配器必须返回小写键（与 SQLAlchemy 适配器一致），"
        "否则 evaluator 的 row.get('total') 全部 None 回退 0，"
        "出现 total=0/passed=0/passRate=0/FAIL 的假阴性。"
    )