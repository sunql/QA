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
