"""缓存 Schema job 注册表：并发保护 / 不可变更新 / 惰性淘汰。"""

from __future__ import annotations

import asyncio

from app.domain.exceptions import DataSourceError
from app.services import schema_introspect_job as jobModule
from app.services.schema_introspect_job import (
    SchemaIntrospectJobRegistry,
    _userFacingError,
)


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


def test_create_or_get_does_not_reuse_succeeded_job() -> None:
    """已 succeeded 的 job 不得被复用；TTL 淘汰后再调得到第三个新 jobId。"""
    clock = _FakeClock()
    registry = SchemaIntrospectJobRegistry(ttlSeconds=600.0, clock=clock)

    first = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    registry.update(first.jobId, status="succeeded")

    second = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    assert second.jobId != first.jobId, "终态 (succeeded) job 不得被 createOrGet 复用"
    assert second.status == "running"

    registry.update(second.jobId, status="succeeded")
    clock.advance(601)

    third = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    assert third.jobId != first.jobId
    assert third.jobId != second.jobId, "淘汰后应得到第三个新 jobId"
    assert third.status == "running"


def test_create_or_get_does_not_reuse_failed_job() -> None:
    """已 failed 的 job 同样不得被复用。"""
    registry = SchemaIntrospectJobRegistry(clock=_FakeClock())

    first = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    registry.update(first.jobId, status="failed")

    second = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    assert second.jobId != first.jobId, "终态 (failed) job 不得被 createOrGet 复用"
    assert second.status == "running"


def test_find_running_returns_running_job_for_target() -> None:
    """有 running job ⇒ findRunning 返回该 job（纯查询，不新建）。"""
    registry = SchemaIntrospectJobRegistry(clock=_FakeClock())
    job = registry.createOrGet(datasourceId=7, schemaName="LC019999")

    found = registry.findRunning(datasourceId=7, schemaName="LC019999")

    assert found is not None
    assert found.jobId == job.jobId
    assert found.status == "running"


def test_find_running_returns_none_when_no_running_job() -> None:
    """无 running job ⇒ None，且不得创建新 job（不改变注册表）。"""
    registry = SchemaIntrospectJobRegistry(clock=_FakeClock())

    assert registry.findRunning(datasourceId=7, schemaName="LC019999") is None
    # findRunning 本身不产生 job；createOrGet 才创建（这里置终态以验证终态不被返回）
    registry.update(
        registry.createOrGet(datasourceId=7, schemaName="LC019999").jobId,
        status="succeeded",
    )
    assert registry.findRunning(datasourceId=7, schemaName="LC019999") is None
    fresh = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    assert fresh.status == "running"


def test_find_running_ignores_terminal_jobs() -> None:
    """succeeded / failed 终态不被 findRunning 返回。"""
    registry = SchemaIntrospectJobRegistry(clock=_FakeClock())

    succeeded = registry.createOrGet(datasourceId=7, schemaName="A")
    registry.update(succeeded.jobId, status="succeeded")
    failed = registry.createOrGet(datasourceId=7, schemaName="B")
    registry.update(failed.jobId, status="failed", error="x")

    assert registry.findRunning(datasourceId=7, schemaName="A") is None
    assert registry.findRunning(datasourceId=7, schemaName="B") is None


def test_find_running_scoped_to_datasource_and_schema() -> None:
    """只按 (datasourceId, schemaName) 精确命中，不同目标不串。"""
    registry = SchemaIntrospectJobRegistry(clock=_FakeClock())
    registry.createOrGet(datasourceId=7, schemaName="LC019999")

    assert registry.findRunning(datasourceId=8, schemaName="LC019999") is None
    assert registry.findRunning(datasourceId=7, schemaName="THBI") is None


def test_update_missing_job_raises_key_error() -> None:
    registry = SchemaIntrospectJobRegistry(clock=_FakeClock())
    try:
        registry.update("nope", completed=1)
    except KeyError:
        return
    raise AssertionError("未知 jobId 必须抛 KeyError")


# ---- running 僵尸判据（活性超时） ----


def test_create_or_get_replaces_stale_running_job() -> None:
    """running job 超过 staleTimeoutSeconds 未更新 ⇒ 视为僵尸：移除并给新 jobId。

    触发面：客户端提前断开导致 background 未执行、ownSession() 开会话即失败等
    ⇒ updatedAt 再也不会被刷新。若永久复用，该数据源进程内无法再发起内省。
    """
    clock = _FakeClock()
    registry = SchemaIntrospectJobRegistry(staleTimeoutSeconds=900.0, clock=clock)

    stale = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    clock.advance(901)

    assert registry.findRunning(datasourceId=7, schemaName="LC019999") is None
    assert registry.get(stale.jobId) is None, "僵尸 job 必须从注册表移除"

    fresh = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    assert fresh.jobId != stale.jobId, "僵尸 job 不得被复用"
    assert fresh.status == "running"


def test_create_or_get_reuses_recently_updated_running_job() -> None:
    """仍在推进（updatedAt 被 update 刷新）的 job 即使创建已久也不得误判为僵尸。"""
    clock = _FakeClock()
    registry = SchemaIntrospectJobRegistry(staleTimeoutSeconds=900.0, clock=clock)

    job = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    clock.advance(901)  # 距创建已超 900s
    registry.update(job.jobId, completed=1)  # 进度刷新 updatedAt

    clock.advance(899)  # 距上次刷新未超限
    again = registry.createOrGet(datasourceId=7, schemaName="LC019999")
    assert again.jobId == job.jobId, "刚刷新过的 running job 必须被复用"


def test_run_job_marks_failed_when_own_session_raises(monkeypatch) -> None:
    """ownSession() 抛错也必须落 failed（会话创建挪进 try）。"""
    import app.infrastructure.database as databaseModule

    registry = SchemaIntrospectJobRegistry(clock=_FakeClock())
    job = registry.createOrGet(datasourceId=7, schemaName="LC019999")

    def _boom():
        raise RuntimeError("连接池初始化失败")

    monkeypatch.setattr(databaseModule, "ownSession", _boom)

    asyncio.run(
        jobModule.runIntrospectJob(
            datasourceId=7,
            schemaName="LC019999",
            jobId=job.jobId,
            registry=registry,
        )
    )

    final = registry.get(job.jobId)
    assert final.status == "failed", "ownSession 失败不得让 job 永挂 running"
    assert final.error, "failed 必须带用户可读文案"


# ---- M3：错误文案保留可操作 detail，且不泄漏底层信息 ----


def test_user_facing_error_includes_actionable_detail_without_leaking_cause() -> None:
    """DataSourceError 的 detail 必须拼进 error，但 __cause__ 里的敏感文本不得出现。"""
    try:
        try:
            raise RuntimeError("连接 LC019999@h:1521/X 失败：业务库不可达")
        except RuntimeError as cause:
            raise DataSourceError(
                "数据源 7 schema 读取失败",
                detail="业务数据库读取失败，请检查数据源连接配置",
            ) from cause
    except DataSourceError as exc:
        text = _userFacingError(exc)

    assert "业务数据库读取失败，请检查数据源连接配置" in text, "必须保留可操作提示"
    assert "LC019999" not in text
    assert "业务库不可达" not in text
    assert "h:1521" not in text


def test_user_facing_error_without_detail_falls_back_to_message() -> None:
    exc = DataSourceError("数据源 7 schema 读取失败")
    assert _userFacingError(exc) == "数据源 7 schema 读取失败"

