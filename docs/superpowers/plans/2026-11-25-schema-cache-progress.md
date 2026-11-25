# 缓存 Schema 进度弹窗 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把数据源页「缓存 Schema」从无反馈的同步长请求，改为「分批内省 + 内存 job + 前端轮询进度弹窗」，正常进行中永不判超时。

**Architecture:** 后端新增进程内 job 注册表与分批内省（100 表/批，五类元数据查询均带 `table_name IN (...)` 过滤），用 `BackgroundTasks` + `ownSession()` 异步执行；新增两个端点（启动 job / 查询进度）。前端 DatasourcePage 打开 Modal 轮询进度，弹窗可关闭后台继续。旧同步接口保持不变。

**Tech Stack:** FastAPI + SQLAlchemy async + pytest（真实 PostgreSQL 集成）/ React + antd + TypeScript + vitest。

**Spec:** `docs/superpowers/specs/2026-11-25-schema-cache-progress-design.md`

## Global Constraints

- 不可变数据：job 状态更新返回新实例（`dataclasses.replace`），禁止原地修改。
- 显式错误处理：job 失败必须落 `status=failed` + 用户可读 error 文案；底层异常文本不得泄露 DSN/用户名。
- 失败不写缓存：任何一批查询失败 ⇒ 整次内省不写 `schema_cache`（保持原子性）。
- 后端测试必须用真实 PostgreSQL（`TEST_DATABASE_URL` 指向 `qa_metadata_test*`），禁止 sqlite。
- 文件规模：单个文件 200–400 行为宜，超 800 行必须拆分。本次新建的 job 注册表独立成文件。
- 前端：不使用 `.at()`（tsconfig lib 不含 es2022），用 `arr[arr.length - 1]`。

---

### Task 1: 内存 job 注册表

**Files:**
- Create: `backend/app/services/schema_introspect_job.py`
- Test: `backend/app/tests/unit/test_schema_introspect_job.py`

**Interfaces:**
- Produces:
  - `SchemaIntrospectJob`（frozen dataclass）：字段 `jobId: str`、`datasourceId: int`、`schemaName: str`、`status: str`（`running`|`succeeded`|`failed`）、`total: int`、`completed: int`、`error: str | None`、`updatedAt: float`
  - `SchemaIntrospectJobRegistry.createOrGet(*, datasourceId: int, schemaName: str) -> SchemaIntrospectJob`
  - `SchemaIntrospectJobRegistry.update(jobId: str, *, total: int | None = None, completed: int | None = None, status: str | None = None, error: str | None = None) -> SchemaIntrospectJob`
  - `SchemaIntrospectJobRegistry.get(jobId: str) -> SchemaIntrospectJob | None`
  - 模块级单例 `schema_introspect_job_registry`

- [ ] **Step 1: 写失败测试**

```python
"""缓存 Schema job 注册表：并发保护 / 不可变更新 / 惰性淘汰。"""

from __future__ import annotations

from app.services.schema_introspect_job import SchemaIntrospectJobRegistry


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_create_or_get_returns_same_running_job_for_same_target() -> None:
    """同一 (datasourceId, schemaName) 已有 running job ⇒ 返回同一 jobId（不重复启动）。"""
    registry = SchemaIntrospectJobRegistry(clock=_FakeClock())

    first = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    second = registry.createOrGet(datasourceId=7, schemaName="LC019999")

    assert first.jobId == second.jobId
    assert first.status == "running"


def test_create_or_get_allows_different_targets() -> None:
    registry = SchemaIntrospectJobRegistry(clock=_FakeClock())

    a = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    b = registry.createOrGet(datasourceId=7, schemaName="THBI")
    c = registry.createOrGet(datasourceId=8, schemaName="LC019999")

    assert len({a.jobId, b.jobId, c.jobId}) == 3


def test_update_returns_new_immutable_instance() -> None:
    """更新返回新实例，原实例不变（不可变数据约束）。"""
    registry = SchemaIntrospectJobRegistry(clock=_FakeClock())
    job = registry.createOrGet(datasourceId=7, schemaName="X")

    updated = registry.update(job.jobId, total=860, completed=100)

    assert job.total == 0 and job.completed == 0, "原实例不得被原地修改"
    assert updated.total == 860 and updated.completed == 100
    assert registry.get(job.jobId).completed == 100


def test_terminal_job_replaced_after_ttl_not_before() -> None:
    """终态 job 在 TTL 后才淘汰；running job 永不淘汰。"""
    clock = _FakeClock()
    registry = SchemaIntrospectJobRegistry(ttlSeconds=600.0, clock=clock)

    done = registry.createOrGet(datasourceId=1, schemaName="A")
    registry.update(done.jobId, status="succeeded")
    running = registry.createOrGet(datasourceId=2, schemaName="B")

    clock.advance(599)
    assert registry.get(done.jobId) is not None
    clock.advance(2)
    assert registry.get(done.jobId) is None, "终态 job 超 TTL 应被淘汰"
    assert registry.get(running.jobId) is not None, "running job 不得淘汰"


def test_update_missing_job_raises_key_error() -> None:
    registry = SchemaIntrospectJobRegistry(clock=_FakeClock())
    try:
        registry.update("nope", completed=1)
    except KeyError:
        return
    raise AssertionError("未知 jobId 必须抛 KeyError")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest app/tests/unit/test_schema_introspect_job.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.schema_introspect_job'`

- [ ] **Step 3: 写实现**

```python
"""缓存 Schema 异步任务的进程内注册表。

设计见 docs/superpowers/specs/2026-11-25-schema-cache-progress-design.md：
- 纯内存（单进程 uvicorn 假设；横向扩容需换共享存储，另批处理）；
- 不可变：update 返回新实例，绝不原地改既有 job；
- 惰性淘汰：每次 create/get 顺带清理超过 TTL 的终态 job；
- 并发保护：同一 (datasourceId, schemaName) 只允许一个 running job。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

JobStatus = Literal["running", "succeeded", "failed"]

_TERMINAL_STATUSES = frozenset({"succeeded", "failed"})


@dataclass(frozen=True)
class SchemaIntrospectJob:
    """一次异步内省任务的快照（不可变）。"""

    jobId: str
    datasourceId: int
    schemaName: str
    status: JobStatus
    total: int = 0
    completed: int = 0
    error: str | None = None
    updatedAt: float = 0.0


class SchemaIntrospectJobRegistry:
    """进程内 job 表：创建 / 更新 / 查询 / 惰性淘汰。"""

    def __init__(
        self,
        *,
        ttlSeconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._jobs: dict[str, SchemaIntrospectJob] = {}
        self._ttlSeconds = ttlSeconds
        self._clock = clock

    def createOrGet(
        self, *, datasourceId: int, schemaName: str
    ) -> SchemaIntrospectJob:
        """返回该目标上仍在运行的 job；没有则新建一个。"""
        self._evict()
        for job in self._jobs.values():
            if (
                job.datasourceId == datasourceId
                and job.schemaName == schemaName
                and job.status == "running"
            ):
                return job
        created = SchemaIntrospectJob(
            jobId=uuid.uuid4().hex,
            datasourceId=datasourceId,
            schemaName=schemaName,
            status="running",
            updatedAt=self._clock(),
        )
        self._jobs[created.jobId] = created
        return created

    def update(
        self,
        jobId: str,
        *,
        total: int | None = None,
        completed: int | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> SchemaIntrospectJob:
        """写入部分字段并返回新快照（不原地修改）。"""
        current = self._jobs.get(jobId)
        if current is None:
            raise KeyError(f"未找到 job: {jobId}")
        changes: dict[str, object] = {"updatedAt": self._clock()}
        if total is not None:
            changes["total"] = total
        if completed is not None:
            changes["completed"] = completed
        if status is not None:
            changes["status"] = status
        if error is not None:
            changes["error"] = error
        updated = replace(current, **changes)
        self._jobs[jobId] = updated
        return updated

    def get(self, jobId: str) -> SchemaIntrospectJob | None:
        self._evict()
        return self._jobs.get(jobId)

    def _evict(self) -> None:
        """惰性淘汰：只清终态且超 TTL 的 job（running 永不清）。"""
        now = self._clock()
        stale = [
            jobId
            for jobId, job in self._jobs.items()
            if job.status in _TERMINAL_STATUSES
            and now - job.updatedAt > self._ttlSeconds
        ]
        for jobId in stale:
            del self._jobs[jobId]


#: 进程级单例（API 与后台任务共用同一份状态）
schema_introspect_job_registry = SchemaIntrospectJobRegistry()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest app/tests/unit/test_schema_introspect_job.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/schema_introspect_job.py backend/app/tests/unit/test_schema_introspect_job.py
git commit -m "feat(datasource): schema 内省 job 注册表（内存态 + 惰性淘汰 + 并发保护）"
```

---

### Task 2: 分批内省（表名列表 + 分批查询 + 进度回调）

**Files:**
- Modify: `backend/app/services/schema_introspection_service.py`
- Test: `backend/app/tests/unit/test_schema_introspection_batched.py`

**Interfaces:**
- Consumes: 现有 `_mergeRows` / `_annotateComments` / `_assembleTables` / `_normalizeSchemaName`
- Produces:
  - `_sqlLiteral(value: str) -> str` — 转义单引号并加引号
  - `_tableNamesSql(dsType: DataSourceType, owner: str) -> str`
  - `SchemaIntrospectionService.introspectBatched(ds, *, owner, onProgress: Callable[[int, int], None], batchSize: int = 100) -> list[TableSchemaRead]`
  - `SchemaIntrospectionService.introspectAndCacheBatched(session, ds, *, owner, onProgress, jobId, registry) -> SchemaCache`

- [ ] **Step 1: 写失败测试（分批等价性 + 进度序列）**

```python
"""分批内省：多批合并结果必须与单次全量一致，进度回调按批推进。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import DataSourceType
from app.services.schema_introspection_service import SchemaIntrospectionService


def _ds(dsType: str = "oracle"):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=11,
        type=dsType,
        username="LC019999",
        host="h",
        port=1521,
        databaseName="X",
    )


class _BatchedAdapter:
    """按 SQL 中的 table_name IN (...) 过滤返回；记录每次查询供断言。"""

    def __init__(self, tableNames: list[str]) -> None:
        self.executed: list[str] = []
        self.tableNames = tableNames

    async def execute_read_only(self, sql: str) -> list[dict]:
        self.executed.append(sql)
        if "SELECT table_name FROM ALL_TABLES" in sql:
            return [{"table_name": t} for t in self.tableNames]
        picked = [t for t in self.tableNames if f"'{t}'" in sql]
        if "ALL_TAB_COLUMNS" in sql:
            return [
                {
                    "table_name": t,
                    "column_name": "COL1",
                    "data_type": "VARCHAR2",
                    "nullable": 0,
                    "owner": "LC019999",
                }
                for t in picked
            ]
        if "constraint_type = 'P'" in sql:
            return [{"table_name": t, "column_name": "COL1"} for t in picked]
        return []


def test_batched_merge_equals_full_and_progress_monotonic() -> None:
    names = [f"T{i:03d}" for i in range(250)]
    adapter = _BatchedAdapter(names)
    service = SchemaIntrospectionService(
        adapterProvider=lambda dsId, ds: adapter
    )
    seen: list[tuple[int, int]] = []

    tables = asyncio_run(
        service.introspectBatched(
            _ds(), owner="LC019999",
            onProgress=lambda done, total: seen.append((done, total)),
            batchSize=100,
        )
    )

    assert len(tables) == 250, "分批不得丢表"
    assert seen[0] == (0, 250), "首个回调必须在首批之前报 (0, total)"
    assert seen[-1] == (250, 250), "末个回调必须报满"
    assert [d for d, _ in seen] == sorted(d for d, _ in seen), "进度必须单调不减"
    assert all(t == 250 for _, t in seen)


def test_batched_query_count_is_one_per_type_per_batch() -> None:
    """250 表 / 100 一批 ⇒ 3 批；每批 5 类查询 + 1 次表名列表 = 16 次。"""
    adapter = _BatchedAdapter([f"T{i:03d}" for i in range(250)])
    service = SchemaIntrospectionService(adapterProvider=lambda dsId, ds: adapter)

    asyncio_run(
        service.introspectBatched(
            _ds(), owner="LC019999", onProgress=lambda d, t: None, batchSize=100
        )
    )

    tableListCalls = [s for s in adapter.executed if "SELECT table_name FROM ALL_TABLES" in s]
    assert len(tableListCalls) == 1, "表名列表只查一次"
```

> 说明：文件顶部加 `from asyncio import run as asyncio_run`。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest app/tests/unit/test_schema_introspection_batched.py -v`
Expected: FAIL — `AttributeError: 'SchemaIntrospectionService' object has no attribute 'introspectBatched'`

- [ ] **Step 3: 写实现**

在 `schema_introspection_service.py` 中新增（放在 `introspect` 方法之后）：

```python
    async def introspectBatched(
        self,
        ds: DataSource,
        *,
        owner: str | None = None,
        onProgress: Callable[[int, int], None],
        batchSize: int = 100,
    ) -> list[TableSchemaRead]:
        """按表分批内省，每批结束回调 (已覆盖表数, 总表数)。

        与 introspect 的差别只有执行粒度：先把表名列表查出来，再按 batchSize
        切批，五类元数据查询均追加 table_name IN (...) 过滤；合并逻辑
        （_mergeRows / _annotateComments / _assembleTables）与全量路径完全一致
        ⇒ 结果等价（见 test_batched_merge_equals_full_and_progress_monotonic）。
        """
        adapter = self._adapterProvider(ds.id, ds)
        dsType = _dataSourceType(ds)
        schemaName = _normalizeSchemaName(ds, owner)
        try:
            tableNames = await self._listTableNames(adapter, ds, dsType, schemaName)
            total = len(tableNames)
            onProgress(0, total)
            merged: dict[str, dict] = {}
            for start in range(0, total, max(1, batchSize)):
                batch = tableNames[start : start + batchSize]
                merged.update(await self._queryBatch(adapter, ds, dsType, schemaName, batch))
                onProgress(min(start + len(batch), total), total)
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - 与 introspect 同口径包装
            logger.error("数据源 %s 分批 schema 读取失败: %s", ds.id, exc, exc_info=True)
            raise DataSourceError(
                MSG_DATASOURCE_SCHEMA_READ_FAILED.format(datasourceId=ds.id),
                detail=MSG_DATASOURCE_SCHEMA_READ_FAILED_DETAIL,
            ) from exc
        tables = self._assembleTables(merged)
        maxTables = getSettings().schemaMaxTables
        if len(tables) > maxTables:
            raise DataSourceError(
                MSG_DATASOURCE_SCHEMA_TABLE_LIMIT_EXCEEDED.format(
                    datasourceId=ds.id, tableCount=len(tables), maxTables=maxTables
                ),
                detail=MSG_DATASOURCE_SCHEMA_TABLE_LIMIT_DETAIL,
            )
        return tables

    @staticmethod
    async def _listTableNames(
        adapter: BusinessDbAdapter,
        ds: DataSource,
        dsType: DataSourceType,
        schemaName: str,
    ) -> list[str]:
        rows = await adapter.execute_read_only(_tableNamesSql(dsType, schemaName))
        return [str(r.get("table_name") or "") for r in rows if r.get("table_name")]

    async def _queryBatch(
        self,
        adapter: BusinessDbAdapter,
        ds: DataSource,
        dsType: DataSourceType,
        schemaName: str,
        tableNames: list[str],
    ) -> dict[str, dict]:
        """执行一批表的五类查询并合并；SQL 形态按数据源类型选择。"""
        plain = _tableNameInClause(tableNames)  # 单表查询 / 注释查询
        if dsType is DataSourceType.ORACLE:
            columnRows = await adapter.execute_read_only(
                _ORACLE_COLUMNS_SQL.format(owner=schemaName) + plain
            )
            pkRows = await adapter.execute_read_only(
                _ORACLE_PK_SQL.format(owner=schemaName)
                + _tableNameInClause(tableNames, prefix="c.")
            )
            fkRows = await adapter.execute_read_only(
                _ORACLE_FK_SQL.format(owner=schemaName)
                + _tableNameInClause(tableNames, prefix="cc.")
            )
            tableComments = await adapter.execute_read_only(
                _ORACLE_TABLE_COMMENT_SQL.format(owner=schemaName) + plain
            )
            colComments = await adapter.execute_read_only(
                _ORACLE_COL_COMMENT_SQL.format(owner=schemaName) + plain
            )
        elif dsType is DataSourceType.POSTGRESQL:
            columnRows = await adapter.execute_read_only(_PG_SQL["columns"] + plain)
            pkRows = await adapter.execute_read_only(
                _PG_SQL["primary_keys"] + _tableNameInClause(tableNames, prefix="tc.")
            )
            fkRows = await adapter.execute_read_only(
                _PG_SQL["foreign_keys"] + _tableNameInClause(tableNames, prefix="tc.")
            )
            tableComments = await adapter.execute_read_only(_PG_TABLE_COMMENT_SQL + plain)
            colComments = await adapter.execute_read_only(_PG_COL_COMMENT_SQL + plain)
        else:
            columnRows = await adapter.execute_read_only(_MYSQL_SQL["columns"] + plain)
            pkRows = await adapter.execute_read_only(
                _MYSQL_SQL["primary_keys"] + _tableNameInClause(tableNames, prefix="tc.")
            )
            fkRows = await adapter.execute_read_only(
                _MYSQL_SQL["foreign_keys"] + _tableNameInClause(tableNames, prefix="tc.")
            )
            tableComments = await adapter.execute_read_only(
                _MYSQL_TABLE_COMMENT_SQL + plain
            )
            colComments = await adapter.execute_read_only(_MYSQL_COL_COMMENT_SQL + plain)
        merged = _mergeRows(columnRows, pkRows, fkRows)
        _annotateComments(merged, tableComments, colComments)
        return merged
```

模块级辅助函数（放在 `_normalizeSchemaName` 附近）：

```python
def _sqlLiteral(value: str) -> str:
    """把字符串渲染为 SQL 字面量（单引号双写转义）。

    表名来自数据库元数据字典表（非用户输入），仍做字面量转义防异常字符。
    """
    return "'" + str(value).replace("'", "''") + "'"


def _tableNameInClause(tableNames: list[str], *, prefix: str = "") -> str:
    """追加 AND <prefix>table_name IN (...)，用于把五类查询限定在本批表内。

    prefix 必填于 JOIN 查询：PK/FK 的 SQL 里有多张表，裸 `table_name` 会歧义
    （Oracle 报 ORA-00918，MySQL 报 1052）。单表查询用默认空前缀。
    """
    if not tableNames:
        return " AND 1 = 0"
    joined = ", ".join(_sqlLiteral(name) for name in tableNames)
    column = f"{prefix}table_name" if prefix else "table_name"
    return f" AND {column} IN ({joined})"


def _dataSourceType(ds: DataSource) -> DataSourceType:
    try:
        return DataSourceType(ds.type)
    except ValueError as exc:
        raise ValidationError(
            MSG_DATASOURCE_TYPE_NOT_SUPPORTED.format(dsType=ds.type)
        ) from exc


def _tableNamesSql(dsType: DataSourceType, owner: str) -> str:
    """列出该 schema 下的业务表名（轻查询，分批的批次依据）。"""
    if dsType is DataSourceType.ORACLE:
        return (
            "SELECT table_name FROM ALL_TABLES "
            f"WHERE owner = '{owner}' ORDER BY table_name"
        )
    if dsType is DataSourceType.POSTGRESQL:
        return (
            "SELECT c.relname AS table_name FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = CURRENT_SCHEMA() AND c.relkind = 'r' "
            "ORDER BY c.relname"
        )
    return (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
        "ORDER BY table_name"
    )
```

> **别名要点（已体现在上面的实现里）**：PK/FK 是 JOIN 查询，裸 `table_name` 会歧义
> （Oracle ORA-00918 / MySQL 1052）⇒ 分别用 `prefix="c."` / `"cc."` / `"tc."`。
> 单表查询与注释查询用默认空前缀。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest app/tests/unit/test_schema_introspection_batched.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/schema_introspection_service.py backend/app/tests/unit/test_schema_introspection_batched.py
git commit -m "feat(datasource): 分批内省 —— 100 表/批 + 进度回调，结果与全量等价"
```

---

### Task 3: 异步执行并写缓存（失败不写）

**Files:**
- Modify: `backend/app/services/schema_introspection_service.py`
- Modify: `backend/app/services/schema_introspect_job.py`（新增执行入口）
- Test: `backend/app/tests/integration/test_schema_introspect_job_api.py`

**Interfaces:**
- Consumes: Task 1 的 `SchemaIntrospectJobRegistry`、Task 2 的 `introspectBatched`
- Produces: `runIntrospectJob(*, datasourceId: int, schemaName: str, jobId: str, registry: SchemaIntrospectJobRegistry) -> None`（在 `schema_introspect_job.py` 中，供 API 的 BackgroundTasks 调用）

- [ ] **Step 1: 写失败测试**

```python
"""异步内省 job：成功写缓存 / 失败不写 / 状态流转。"""

from __future__ import annotations

import pytest

from app.services import schema_introspect_job as jobModule
from app.services.schema_introspect_job import SchemaIntrospectJobRegistry


@pytest.mark.asyncio
async def test_run_job_success_writes_cache(monkeypatch, client) -> None:
    """成功路径：job 变 succeeded，schema 缓存可读。"""
    resp = await client.post(
        "/api/v1/datasources",
        json={
            "name": "ds", "type": "oracle", "host": "h", "port": 1521,
            "databaseName": "X", "username": "LC019999", "password": "p",
            "isActive": True, "isDefault": False,
        },
    )
    dsId = resp.json()["id"]

    class _OkAdapter:
        async def execute_read_only(self, sql: str) -> list[dict]:
            if "SELECT table_name FROM ALL_TABLES" in sql:
                return [{"table_name": "PRECEIPT"}]
            if "ALL_TAB_COLUMNS" in sql:
                return [{
                    "table_name": "PRECEIPT", "column_name": "COL1",
                    "data_type": "VARCHAR2", "nullable": 0, "owner": "LC019999",
                }]
            return []

    monkeypatch.setattr(jobModule, "_adapterProvider", lambda dsId, ds: _OkAdapter())
    registry = SchemaIntrospectJobRegistry()
    job = registry.createOrGet(datasourceId=dsId, schemaName="LC019999")

    await jobModule.runIntrospectJob(
        datasourceId=dsId, schemaName="LC019999", jobId=job.jobId, registry=registry
    )

    final = registry.get(job.jobId)
    assert final.status == "succeeded"
    assert final.total == 1 and final.completed == 1

    cached = await client.get(f"/api/v1/datasources/{dsId}/schema?schema=LC019999")
    assert cached.status_code == 200
    assert cached.json()["tables"][0]["tableName"] == "PRECEIPT"


@pytest.mark.asyncio
async def test_run_job_failure_marks_failed_and_writes_nothing(monkeypatch, client) -> None:
    """失败路径：job failed + error；schema_cache 不得出现半成品。"""
    resp = await client.post(
        "/api/v1/datasources",
        json={
            "name": "ds2", "type": "oracle", "host": "h", "port": 1521,
            "databaseName": "X", "username": "LC019999", "password": "p",
            "isActive": True, "isDefault": False,
        },
    )
    dsId = resp.json()["id"]

    class _BoomAdapter:
        async def execute_read_only(self, sql: str) -> list[dict]:
            raise ConnectionError("业务库不可达")

    monkeypatch.setattr(jobModule, "_adapterProvider", lambda dsId, ds: _BoomAdapter())
    registry = SchemaIntrospectJobRegistry()
    job = registry.createOrGet(datasourceId=dsId, schemaName="LC019999")

    await jobModule.runIntrospectJob(
        datasourceId=dsId, schemaName="LC019999", jobId=job.jobId, registry=registry
    )

    final = registry.get(job.jobId)
    assert final.status == "failed"
    assert final.error and "读取失败" in final.error

    cached = await client.get(f"/api/v1/datasources/{dsId}/schema?schema=LC019999")
    assert cached.status_code == 404, "失败不得写缓存"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest app/tests/integration/test_schema_introspect_job_api.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'runIntrospectJob'`

- [ ] **Step 3: 写实现**

在 `schema_introspect_job.py` 末尾追加：

```python
# --------------------------------------------------------------------------
# 后台执行入口
# --------------------------------------------------------------------------

#: 适配器提供者（测试可 monkeypatch；生产走 business_db_pool.get_adapter）
def _defaultAdapterProvider(dsId: int, ds):
    from app.infrastructure.business_db_pool import get_adapter

    return get_adapter(dsId, ds)


_adapterProvider = _defaultAdapterProvider


async def runIntrospectJob(
    *,
    datasourceId: int,
    schemaName: str,
    jobId: str,
    registry: SchemaIntrospectJobRegistry,
) -> None:
    """在后台会话中跑一次分批内省并写缓存，全程维护 job 状态。

    三条硬约束：
    1. 用 ownSession() 自开会话（后台任务复用请求会话会触发 MissingGreenlet，
       见 infrastructure/database.py 的 2026-11-16 事故说明）；
    2. 任一批失败 ⇒ 不写缓存（introspectAndCacheBatched 内聚在单个事务里）；
    3. 对外只暴露友好文案，底层异常文本只进服务端日志。
    """
    from app.infrastructure.database import ownSession
    from app.services.datasource_service import DataSourceService
    from app.services.schema_introspection_service import SchemaIntrospectionService

    session = ownSession()
    try:
        ds = await DataSourceService().get(session, datasourceId)
        service = SchemaIntrospectionService(adapterProvider=_adapterProvider)
        await service.introspectAndCacheBatched(
            session,
            ds,
            owner=schemaName,
            registry=registry,
            jobId=jobId,
        )
    except Exception as exc:  # noqa: BLE001 - 后台任务边界，必须兜住并落状态
        logger.error(
            "schema 内省 job 失败 jobId=%s datasource=%s schema=%s: %s",
            jobId, datasourceId, schemaName, exc, exc_info=True,
        )
        registry.update(jobId, status="failed", error=_userFacingError(exc))
    finally:
        await session.close()
```

在 `schema_introspection_service.py` 中追加：

```python
    async def introspectAndCacheBatched(
        self,
        session: AsyncSession,
        ds: DataSource,
        *,
        owner: str | None,
        registry: Any,
        jobId: str,
    ) -> SchemaCache:
        """分批内省 → 写缓存，并把进度同步到 job 注册表。

        与 introspectAndCache 的写库逻辑完全一致；区别只在：
        1. 内省走 introspectBatched（带进度回调）；
        2. 回调把 (completed, total) 写进 job 注册表。
        失败时异常向上抛，由 runIntrospectJob 统一落 failed（不写缓存）。
        """
        schemaName = _normalizeSchemaName(ds, owner)

        def onProgress(done: int, total: int) -> None:
            registry.update(jobId, total=total, completed=done)

        tables = await self.introspectBatched(
            ds, owner=schemaName, onProgress=onProgress
        )
        schemaData = [t.model_dump() for t in tables]
        version = _schemaVersion(schemaData)
        existing = await self.getCached(session, ds.id, owner=schemaName, ds=ds)
        if existing is not None and existing.schema_version == version:
            return existing
        if existing is None:
            cache = SchemaCache(
                datasource_id=ds.id,
                schema_name=schemaName,
                schema_data=schemaData,
                schema_version=version,
            )
            session.add(cache)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                winner = await self.getCached(session, ds.id, owner=schemaName, ds=ds)
                if winner is not None:
                    return winner
                raise
            await session.refresh(cache)
            return cache
        await session.execute(
            update(SchemaCache)
            .where(
                SchemaCache.datasource_id == ds.id,
                SchemaCache.schema_name == schemaName,
            )
            .values(schema_data=schemaData, schema_version=version, updated_time=datetime.now(UTC))
        )
        await session.commit()
        await session.refresh(existing)
        return existing
```

`_userFacingError(exc)` 辅助函数（在 `schema_introspect_job.py`）：

```python
def _userFacingError(exc: BaseException) -> str:
    """把底层异常映射为用户可读文案（不泄露 DSN / 用户名）。"""
    from app.domain.exceptions import DataSourceError

    if isinstance(exc, DataSourceError):
        return str(exc)
    return "数据源 schema 读取失败：请检查数据源连接是否正常"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest app/tests/integration/test_schema_introspect_job_api.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/schema_introspect_job.py backend/app/services/schema_introspection_service.py backend/app/tests/integration/test_schema_introspect_job_api.py
git commit -m "feat(datasource): 异步内省执行入口 —— 成功写缓存，失败落 failed 且不写"
```

---

### Task 4: API 端点（启动 job / 查询进度）

**Files:**
- Modify: `backend/app/api/v1/datasource.py`
- Test: `backend/app/tests/integration/test_schema_introspect_job_endpoints.py`

**Interfaces:**
- Consumes: Task 1 的 `schema_introspect_job_registry`、Task 3 的 `runIntrospectJob`
- Produces: `POST /api/v1/datasources/{id}/introspect-jobs` → 202 `{jobId, status, total, completed}`；`GET /api/v1/datasources/{id}/introspect-jobs/{jobId}` → 200 同结构

- [ ] **Step 1: 写失败测试**

```python
"""POST/GET /datasources/{id}/introspect-jobs 契约。"""

from __future__ import annotations

from app.services import schema_introspect_job as jobModule

PAYLOAD = {
    "name": "dsj", "type": "oracle", "host": "h", "port": 1521,
    "databaseName": "X", "username": "LC019999", "password": "p",
    "isActive": True, "isDefault": False,
}


class _SlowAdapter:
    """立即返回空表列表的适配器（让 job 快速结束）。"""

    async def execute_read_only(self, sql: str) -> list[dict]:
        return []


async def test_start_job_returns_202_with_job_id(client, monkeypatch) -> None:
    dsId = (await client.post("/api/v1/datasources", json=PAYLOAD)).json()["id"]
    monkeypatch.setattr(jobModule, "_adapterProvider", lambda dsId, ds: _SlowAdapter())

    resp = await client.post(f"/api/v1/datasources/{dsId}/introspect-jobs?schema=LC019999")

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["jobId"]
    assert body["status"] in {"running", "succeeded", "failed"}


async def test_get_job_returns_progress(client, monkeypatch) -> None:
    dsId = (await client.post("/api/v1/datasources", json=PAYLOAD)).json()["id"]
    monkeypatch.setattr(jobModule, "_adapterProvider", lambda dsId, ds: _SlowAdapter())
    jobId = (await client.post(
        f"/api/v1/datasources/{dsId}/introspect-jobs?schema=LC019999"
    )).json()["jobId"]

    resp = await client.get(f"/api/v1/datasources/{dsId}/introspect-jobs/{jobId}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["jobId"] == jobId
    assert {"status", "total", "completed"} <= body.keys()


async def test_get_unknown_job_404(client) -> None:
    dsId = (await client.post("/api/v1/datasources", json=PAYLOAD)).json()["id"]
    resp = await client.get(f"/api/v1/datasources/{dsId}/introspect-jobs/nonexistent")
    assert resp.status_code == 404


async def test_job_of_other_datasource_404(client, monkeypatch) -> None:
    """jobId 归属另一数据源 ⇒ 404（不泄露他人 job）。"""
    dsA = (await client.post("/api/v1/datasources", json={**PAYLOAD, "name": "a"})).json()["id"]
    dsB = (await client.post("/api/v1/datasources", json={**PAYLOAD, "name": "b"})).json()["id"]
    monkeypatch.setattr(jobModule, "_adapterProvider", lambda dsId, ds: _SlowAdapter())
    jobId = (await client.post(
        f"/api/v1/datasources/{dsA}/introspect-jobs?schema=LC019999"
    )).json()["jobId"]

    resp = await client.get(f"/api/v1/datasources/{dsB}/introspect-jobs/{jobId}")
    assert resp.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest app/tests/integration/test_schema_introspect_job_endpoints.py -v`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 写实现**

在 `datasource.py` 增加导入与端点（放在 `getCachedSchema` 之后）：

```python
from fastapi import BackgroundTasks

from app.services.schema_introspect_job import (
    runIntrospectJob,
    schema_introspect_job_registry,
)
from app.services.schema_introspection_service import _normalizeSchemaName
```

```python
@router.post(
    "/{datasourceId}/introspect-jobs",
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(_INTROSPECT_LIMIT)
async def startIntrospectJob(
    request: Request,
    datasourceId: int,
    background: BackgroundTasks,
    schema: str | None = Query(default=None, description="Oracle owner 命名空间"),
    session: AsyncSession = Depends(getDb),
) -> dict:
    """启动异步 schema 内省任务；同一目标已在跑则返回既有 jobId。

    立即返回 202 + jobId，进度由 GET .../introspect-jobs/{jobId} 轮询。
    后台任务用自有会话执行（见 runIntrospectJob 的会话隔离说明）。
    """
    ds = await _service.get(session, datasourceId)
    schemaName = _normalizeSchemaName(ds, schema)
    job = schema_introspect_job_registry.createOrGet(
        datasourceId=ds.id, schemaName=schemaName
    )
    if job.completed == 0 and job.total == 0:
        # 只有新建的 job 才落后台任务；拿回既有 running job 时不重复启动
        background.add_task(
            runIntrospectJob,
            datasourceId=ds.id,
            schemaName=schemaName,
            jobId=job.jobId,
            registry=schema_introspect_job_registry,
        )
    return {
        "jobId": job.jobId,
        "status": job.status,
        "total": job.total,
        "completed": job.completed,
    }


@router.get("/{datasourceId}/introspect-jobs/{jobId}")
async def getIntrospectJob(datasourceId: int, jobId: str) -> dict:
    """查询异步内省任务进度；不存在 / 不属于该数据源 / 已淘汰 ⇒ 404。"""
    job = schema_introspect_job_registry.get(jobId)
    if job is None or job.datasourceId != datasourceId:
        raise NotFoundError(f"内省任务不存在或已过期（jobId={jobId}）")
    return {
        "jobId": job.jobId,
        "status": job.status,
        "total": job.total,
        "completed": job.completed,
        "error": job.error,
    }
```

> **注意**：`background.add_task` 在测试客户端（ASGITransport）下会在响应返回后立即执行，
> 因此测试里拿到的 GET 结果可能已是 `succeeded`（断言已兼容两种状态）。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest app/tests/integration/test_schema_introspect_job_endpoints.py -v`
Expected: 4 passed

- [ ] **Step 5: 跑整体回归**

Run: `cd backend && uv run pytest app/tests/integration/test_datasource_schema_api.py app/tests/unit/test_schema_introspect_job.py app/tests/unit/test_schema_introspection_batched.py -v`
Expected: 全绿（旧同步接口不受影响）

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/v1/datasource.py backend/app/tests/integration/test_schema_introspect_job_endpoints.py
git commit -m "feat(datasource): introspect-jobs 端点 —— 202 启动 / 进度轮询 / 归属校验"
```

---

### Task 5: 前端 API 函数

**Files:**
- Modify: `frontend/src/api/datasource.ts`
- Test: `frontend/src/tests/datasourceApi.test.ts`

**Interfaces:**
- Produces:
  - `interface IntrospectJob { jobId: string; status: "running" | "succeeded" | "failed"; total: number; completed: number; error?: string | null }`
  - `startIntrospectJob(id: number, schema?: string | null): Promise<IntrospectJob>`
  - `getIntrospectJob(id: number, jobId: string): Promise<IntrospectJob>`

- [ ] **Step 1: 写失败测试**（追加到 `datasourceApi.test.ts`）

```typescript
  it("startIntrospectJob POST /datasources/:id/introspect-jobs（带 schema）", async () => {
    httpMock.post.mockResolvedValue({
      data: { jobId: "abc", status: "running", total: 0, completed: 0 },
    });
    const job = await startIntrospectJob(11, "LC019999");
    const call = httpMock.post.mock.calls[0];
    expect(call[0]).toBe("/datasources/11/introspect-jobs");
    expect(call[2].params).toEqual({ schema: "LC019999" });
    expect(job.jobId).toBe("abc");
  });

  it("getIntrospectJob GET /datasources/:id/introspect-jobs/:jobId", async () => {
    httpMock.get.mockResolvedValue({
      data: { jobId: "abc", status: "running", total: 860, completed: 200, error: null },
    });
    const job = await getIntrospectJob(11, "abc");
    expect(httpMock.get).toHaveBeenCalledWith("/datasources/11/introspect-jobs/abc");
    expect(job.completed).toBe(200);
    expect(job.total).toBe(860);
  });
```

同时在文件顶部导入列表中加入 `startIntrospectJob, getIntrospectJob`。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/tests/datasourceApi.test.ts`
Expected: FAIL — `startIntrospectJob is not a function`

- [ ] **Step 3: 写实现**（追加到 `datasource.ts`）

```typescript
export interface IntrospectJob {
  jobId: string;
  status: "running" | "succeeded" | "failed";
  total: number;
  completed: number;
  error?: string | null;
}

// 启动异步 schema 内省任务。同一数据源+schema 已在跑时后端返回既有 jobId
// （并发保护），调用方无需自己去重。
export async function startIntrospectJob(
  id: number,
  schema?: string | null,
): Promise<IntrospectJob> {
  const res = await httpClient.post<IntrospectJob>(
    `${BASE}/${id}/introspect-jobs`,
    undefined,
    {
      params: schema ? { schema } : undefined,
      timeout: REQUEST_TIMEOUT_MS,
    },
  );
  return res.data;
}

// 查询内省任务进度。后端只读内存态，正常毫秒级返回 ⇒ 用默认短超时；
// 「正常进行中不判超时」由调用方的轮询逻辑保证（连续失败才判中断）。
export async function getIntrospectJob(
  id: number,
  jobId: string,
): Promise<IntrospectJob> {
  const res = await httpClient.get<IntrospectJob>(
    `${BASE}/${id}/introspect-jobs/${jobId}`,
    { timeout: REQUEST_TIMEOUT_MS },
  );
  return res.data;
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/tests/datasourceApi.test.ts`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/datasource.ts frontend/src/tests/datasourceApi.test.ts
git commit -m "feat(datasource-ui): introspect job API —— 启动与进度查询"
```

---

### Task 6: DatasourcePage 进度弹窗

**Files:**
- Modify: `frontend/src/pages/DatasourcePage.tsx`
- Test: `frontend/src/tests/DatasourcePage.test.tsx`

**Interfaces:**
- Consumes: Task 5 的 `startIntrospectJob` / `getIntrospectJob` / `IntrospectJob`
- Produces: 无对外接口；`handleIntrospect` 改为启动 job 并打开弹窗

- [ ] **Step 1: 写失败测试**（追加到 `DatasourcePage.test.tsx`；先在文件顶部的
  `vi.hoisted` mock 对象里补两个成员 `startIntrospectJob: vi.fn()` 与
  `getIntrospectJob: vi.fn()`，否则 `api.startIntrospectJob` 为 undefined）

```typescript
  it("缓存 Schema：打开进度弹窗并按轮询推进", async () => {
    api.startIntrospectJob.mockResolvedValue({
      jobId: "j1", status: "running", total: 200, completed: 0,
    });
    api.getIntrospectJob
      .mockResolvedValueOnce({ jobId: "j1", status: "running", total: 200, completed: 100 })
      .mockResolvedValueOnce({ jobId: "j1", status: "succeeded", total: 200, completed: 200 });

    renderPage();
    await user.click(await screen.findByRole("button", { name: /缓存 Schema/i }));

    expect(api.startIntrospectJob).toHaveBeenCalled();
    expect(await screen.findByText(/正在缓存 schema/)).toBeTruthy();
    // 成功后自动关闭 + 成功提示
    await waitFor(() => expect(screen.queryByText(/正在缓存 schema/)).toBeNull());
  });

  it("缓存 Schema 失败：弹窗展示错误且不自动关闭", async () => {
    api.startIntrospectJob.mockResolvedValue({
      jobId: "j2", status: "running", total: 10, completed: 0,
    });
    api.getIntrospectJob.mockResolvedValue({
      jobId: "j2", status: "failed", total: 10, completed: 3,
      error: "无法连接数据源",
    });

    renderPage();
    await user.click(await screen.findByRole("button", { name: /缓存 Schema/i }));

    expect(await screen.findByText("无法连接数据源")).toBeTruthy();
  });

  it("轮询连续失败 5 次 ⇒ 提示连接中断并停止", async () => {
    api.startIntrospectJob.mockResolvedValue({
      jobId: "j3", status: "running", total: 10, completed: 0,
    });
    api.getIntrospectJob.mockRejectedValue(new Error("down"));

    renderPage();
    await user.click(await screen.findByRole("button", { name: /缓存 Schema/i }));

    await waitFor(() =>
      expect(screen.getByText(/与服务器连接中断/)).toBeTruthy(),
      { timeout: 15000 },
    );
  });
```

> 测试里若要避免真实等待 1.5s × 5，可用 `vi.useFakeTimers()` 并推进；
> 若沿用真实计时器，`waitFor` 的超时要 ≥ 8s（见上面的 `timeout: 15000`）。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/tests/DatasourcePage.test.tsx`
Expected: FAIL — 找不到「正在缓存 schema」文案

- [ ] **Step 3: 写实现**

在 `DatasourcePage.tsx` 中：

```tsx
import { Modal, Progress } from "antd";
import { startIntrospectJob, getIntrospectJob } from "../api/datasource";
import type { IntrospectJob } from "../api/datasource";

const POLL_INTERVAL_MS = 1500;
const MAX_POLL_FAILURES = 5;

interface ActiveJob {
  datasourceId: number;
  jobId: string;
}
```

组件内新增 state 与轮询效应：

```tsx
  const [activeJob, setActiveJob] = useState<ActiveJob | null>(null);
  const [jobProgress, setJobProgress] = useState<IntrospectJob | null>(null);
  const [pollFailures, setPollFailures] = useState(0);

  // 轮询内省进度：只要请求成功就视为「正常进行中」，永不判超时；
  // 仅连续 MAX_POLL_FAILURES 次失败（与服务器失联）才中断。
  // handle 先声明后赋值：tick 可能在 setInterval 之前就跑到 catch 分支，
  // 直接引用 const handle 会命中 TDZ（ReferenceError）。
  useEffect(() => {
    if (!activeJob) return;
    let cancelled = false;
    let failures = 0;
    let handle: number | undefined;
    const stopPolling = () => {
      if (handle !== undefined) window.clearInterval(handle);
    };
    const tick = async () => {
      try {
        const next = await getIntrospectJob(activeJob.datasourceId, activeJob.jobId);
        if (cancelled) return;
        failures = 0;
        setPollFailures(0);
        setJobProgress(next);
        if (next.status === "succeeded") {
          void message.success(t("toast.schemaCached", { count: next.total }));
          stopPolling();
          setActiveJob(null);
          setJobProgress(null);
        } else if (next.status === "failed") {
          stopPolling(); // 终态失败：停止轮询，错误留在弹窗里
        }
      } catch {
        if (cancelled) return;
        failures += 1;
        setPollFailures(failures);
        if (failures >= MAX_POLL_FAILURES) {
          stopPolling();
          void message.error(t("datasource.introspectConnectionLost"));
        }
      }
    };
    handle = window.setInterval(() => void tick(), POLL_INTERVAL_MS);
    void tick();
    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [activeJob, t]);
```

`handleIntrospect` 改为：

```tsx
  const handleIntrospect = async (record: DataSource) => {
    setIntrospectingId(record.id);
    try {
      const job = await startIntrospectJob(record.id);
      setPollFailures(0);
      setJobProgress(job);
      setActiveJob({ datasourceId: record.id, jobId: job.jobId });
    } catch {
      // 错误已由拦截器提示
    } finally {
      setIntrospectingId(null);
    }
  };
```

弹窗（放在组件 return 的末尾）：

```tsx
      <Modal
        open={activeJob !== null}
        title={t("datasource.cacheSchema")}
        onCancel={() => {
          // 允许关闭，后台任务继续；再次点击按钮会拿回同一 jobId
          setActiveJob(null);
          setJobProgress(null);
        }}
        footer={null}
      >
        {jobProgress?.status === "failed" ? (
          <Alert
            type="error"
            showIcon
            message={jobProgress.error ?? t("datasource.introspectFailed")}
          />
        ) : (
          <>
            <p>
              {t("datasource.introspectProgress", {
                done: jobProgress?.completed ?? 0,
                total: jobProgress?.total ?? 0,
              })}
            </p>
            <Progress
              percent={
                jobProgress && jobProgress.total > 0
                  ? Math.round((jobProgress.completed / jobProgress.total) * 100)
                  : 0
              }
              status="active"
            />
            {pollFailures >= MAX_POLL_FAILURES && (
              <Alert type="warning" showIcon message={t("datasource.introspectConnectionLost")} />
            )}
          </>
        )}
      </Modal>
```

i18n（`zh-CN.ts` + `en-US.ts`，`datasource` 命名空间下）：

```typescript
    introspectProgress: "正在缓存 schema：{done}/{total} 张表",
    introspectFailed: "缓存失败，请检查数据源连接",
    introspectConnectionLost: "与服务器连接中断，请检查网络后重试",
```

```typescript
    introspectProgress: "Caching schema: {done}/{total} tables",
    introspectFailed: "Caching failed — check the datasource connection",
    introspectConnectionLost: "Lost connection to the server. Check your network and retry.",
```

> `Alert` 需从 antd 导入（若该文件未导入）。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/tests/DatasourcePage.test.tsx`
Expected: 全绿（含原有用例）

- [ ] **Step 5: typecheck**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无错误

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/DatasourcePage.tsx frontend/src/tests/DatasourcePage.test.tsx frontend/src/i18n/zh-CN.ts frontend/src/i18n/en-US.ts
git commit -m "feat(datasource-ui): 缓存 Schema 进度弹窗 —— 轮询推进，可关闭后台继续"
```

---

## 验证清单

- [ ] 后端全量相关测试：`uv run pytest app/tests/unit/test_schema_introspect_job.py app/tests/unit/test_schema_introspection_batched.py app/tests/integration/test_schema_introspect_job_api.py app/tests/integration/test_schema_introspect_job_endpoints.py app/tests/integration/test_datasource_schema_api.py -v`
- [ ] 前端：`npx vitest run src/tests/datasourceApi.test.ts src/tests/DatasourcePage.test.tsx`
- [ ] `npx tsc --noEmit`
- [ ] 手工冒烟（本地）：对一个 Oracle 大 owner 点「缓存 Schema」，观察进度从 0 推进到 N，弹窗可关闭后重开拿回同一 job
