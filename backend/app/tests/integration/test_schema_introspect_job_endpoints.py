"""POST/GET /datasources/{id}/introspect-jobs 契约。"""

from __future__ import annotations

from app.services import schema_introspect_job as jobModule
from app.services.schema_introspect_job import schema_introspect_job_registry

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


async def test_second_start_reuses_running_job_without_dispatching(
    client, monkeypatch
) -> None:
    """既有 running job（total/completed 均为 0）⇒ 同一 jobId 且不重复挂后台任务。

    回归 brief 的设计缺陷：不能用 `completed == 0 and total == 0` 推断「新建」，
    刚开始跑、尚未报进度的既有 job 同样为零。
    """
    dsId = (await client.post("/api/v1/datasources", json=PAYLOAD)).json()["id"]
    calls: list[str] = []

    class _CountingAdapter:
        async def execute_read_only(self, sql: str) -> list[dict]:
            calls.append(sql)
            return []

    monkeypatch.setattr(jobModule, "_adapterProvider", lambda dsId, ds: _CountingAdapter())
    seeded = schema_introspect_job_registry.createOrGet(
        datasourceId=dsId, schemaName="LC019999"
    )
    try:
        resp = await client.post(
            f"/api/v1/datasources/{dsId}/introspect-jobs?schema=LC019999"
        )

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["jobId"] == seeded.jobId
        assert body["status"] == "running"
        assert body["total"] == 0 and body["completed"] == 0
        assert calls == [], "既有 running job 不得被重复挂后台任务"
    finally:
        # 注册表是进程级单例且测试库 RESTART IDENTITY ⇒ 必须落终态，
        # 否则同 id 的后续用例会命中这个残留 running job。
        schema_introspect_job_registry.update(
            seeded.jobId, status="failed", error="test cleanup"
        )


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
