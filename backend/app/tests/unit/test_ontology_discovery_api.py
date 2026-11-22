"""本体发现 REST 端点（WP-ONT-AI A1-4）的 HTTP 层单测。

放在 unit 目录的原因：CI 的 **integration 分片按「排序下标 mod 4」轮转**，
新建 integration 测试文件会改变分片归属 ⇒ 制造「假净新增失败」。
unit 分片用路径稳定哈希 ⇒ 新建 unit 文件安全。

覆盖本轮审查的 API 层修复点：

1. `GET /sources` 的 `total` 必须是**全量总数**，不是当前页长度（MEDIUM-1）；
2. `POST /sources` 拒绝未知字段（`extra="forbid"`，sec-L-3）；
3. 复用既有快照时审计动作记 `REUSE` 而不是 `CREATE`（MEDIUM-8）；
4. `POST /snapshots` 有速率限制（sec-M-4）；
5. `/diff` 截断时**优先保留破坏性变更**（MEDIUM-2）。
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from slowapi.errors import RateLimitExceeded
from sqlalchemy import select

from app.api.v1 import ontology_discovery
from app.dependencies import getDb, mustChangePasswordGuard
from app.infrastructure.rate_limit import limiter, rateLimitDep, rateLimitExceededHandler

# ---------------------------------------------------------------- 夹具


@pytest.fixture()
async def apiClient(dbSession):
    """按真实接线（router + mustChangePasswordGuard + rateLimitDep）搭一个探针 app。

    `AUTH_MODE` 默认 stub ⇒ 匿名请求同时通过 `requireAdmin` 与 `getCurrentUser`。
    """

    testApp = FastAPI()
    previousEnabled = limiter.enabled
    limiter.enabled = False
    testApp.state.limiter = limiter
    # 端点内限流抛 RateLimitExceeded ⇒ 需要应用级处理器转 429（生产同款）。
    testApp.add_exception_handler(RateLimitExceeded, rateLimitExceededHandler)
    testApp.include_router(
        ontology_discovery.router,
        prefix="/api/v1/ontology-discovery",
        dependencies=[Depends(mustChangePasswordGuard), Depends(rateLimitDep)],
    )

    async def _overrideDb():
        yield dbSession

    testApp.dependency_overrides[getDb] = _overrideDb
    transport = ASGITransport(app=testApp)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        limiter.enabled = previousEnabled


async def _seedDatasource(session, *, name: str = "API演示源", type: str = "mysql"):
    from app.domain.models import DataSource

    ds = DataSource(
        name=name,
        type=type,
        host="127.0.0.1",
        port=3306,
        database_name="demo",
        username="demo",
        password_encrypted="x",
        is_active=True,
        is_default=False,
    )
    session.add(ds)
    await session.flush()
    return ds


async def _registerSource(client: AsyncClient, session, *, name: str, dsId: int) -> int:
    response = await client.post(
        "/api/v1/ontology-discovery/sources",
        json={"name": name, "datasourceId": dsId, "owner": "张业务"},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


# ---------------------------------------------------------------- total 语义


async def test_发现源列表total是全量总数而非本页长度(apiClient, dbSession):
    """MEDIUM-1：`total=len(reads)` 把「本页条数」当成了「总数」。

    后果：前端分页器认为只有 1 条 ⇒ 翻不到第二页。
    """

    ds = await _seedDatasource(dbSession)
    for index in range(3):
        await _registerSource(apiClient, dbSession, name=f"分页源{index}", dsId=ds.id)

    response = await apiClient.get("/api/v1/ontology-discovery/sources?limit=1")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["rows"]) == 1
    assert payload["total"] == 3


# ---------------------------------------------------------------- DTO 严格性


async def test_登记发现源拒绝未知字段(apiClient, dbSession):
    """sec-L-3：未知字段静默忽略 ⇒ 调用方以为传了、其实没生效。"""

    ds = await _seedDatasource(dbSession)

    response = await apiClient.post(
        "/api/v1/ontology-discovery/sources",
        json={
            "name": "未知字段源",
            "datasourceId": ds.id,
            "owner": "张业务",
            "unknownField": "x",
        },
    )

    assert response.status_code == 422, response.text


async def test_登记发现源拒绝把数据源改成别的库(apiClient, dbSession):
    """`datasource_id` 是授权根：DTO 层就拒（不是服务层才发现）。"""

    ds = await _seedDatasource(dbSession)

    response = await apiClient.post(
        "/api/v1/ontology-discovery/sources",
        json={
            "name": "改根源",
            "datasourceId": ds.id,
            "owner": "张业务",
            "datasource_id": ds.id + 1,
        },
    )

    assert response.status_code == 422, response.text


# ---------------------------------------------------------------- 审计动作


async def test_复用既有快照时审计动作记为复用(apiClient, dbSession, monkeypatch):
    """MEDIUM-8：内容未变 ⇒ 复用既有行（没有新建）⇒ 审计却记 `CREATE` 是错的。"""

    from app.services.discovery_snapshot_service import DiscoverySnapshotService

    ds = await _seedDatasource(dbSession)
    sourceId = await _registerSource(apiClient, dbSession, name="审计源", dsId=ds.id)

    from app.domain.schemas import ColumnSchemaRead, TableSchemaRead

    class _FakeIntrospection:
        async def introspect(self, dsArg, *, owner=None):
            return [
                TableSchemaRead(
                    table_name="订单",
                    owner="",
                    columns=[
                        ColumnSchemaRead(
                            column_name="id", data_type="BIGINT", nullable=False, comment=None
                        )
                    ],
                    primary_keys=["id"],
                    foreign_keys=[],
                    comment=None,
                )
            ]

    monkeypatch.setattr(
        ontology_discovery,
        "_service",
        DiscoverySnapshotService(introspectionService=_FakeIntrospection()),
    )

    first = await apiClient.post(
        f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}"
    )
    assert first.status_code == 200, first.text
    assert first.json()["reused"] is False

    second = await apiClient.post(
        f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}"
    )
    assert second.status_code == 200, second.text
    assert second.json()["reused"] is True

    from app.domain.models import AuditLog

    rows = await dbSession.execute(
        select(AuditLog.action).where(
            AuditLog.entity_type == "metadata_snapshot"
        )
    )
    actions = [row for row in rows.scalars().all()]
    assert "REUSE" in actions, actions
    assert actions.count("CREATE") == 1, actions


# ---------------------------------------------------------------- 速率限制


async def test_采集端点有速率限制(apiClient, dbSession, monkeypatch):
    """sec-M-4：采集是「连接业务库读数据字典」的高成本操作 ⇒ 必须限流。

    对齐 `datasource.py` 的 introspect（10/minute）。

    断言**位置**而非「出现过 429」：只断言出现过 429，限流落在别的分支上
    （比如被全局默认限额拦下、或拦在采集之前）也会绿。
    """

    from app.services.discovery_snapshot_service import DiscoverySnapshotService

    class _FakeIntrospection:
        async def introspect(self, dsArg, *, owner=None):
            from app.domain.schemas import ColumnSchemaRead, TableSchemaRead

            return [
                TableSchemaRead(
                    table_name="订单",
                    owner="",
                    columns=[
                        ColumnSchemaRead(
                            column_name="id", data_type="BIGINT", nullable=False, comment=None
                        )
                    ],
                    primary_keys=["id"],
                    foreign_keys=[],
                    comment=None,
                )
            ]

    monkeypatch.setattr(
        ontology_discovery,
        "_service",
        DiscoverySnapshotService(introspectionService=_FakeIntrospection()),
    )

    limiter.enabled = True
    limiter.reset()
    try:
        ds = await _seedDatasource(dbSession)
        sourceId = await _registerSource(apiClient, dbSession, name="限流源", dsId=ds.id)

        statuses = []
        for _ in range(12):
            response = await apiClient.post(
                f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}"
            )
            statuses.append(response.status_code)

        # 前 10 次放行（第 11 次触发），且第 11 次必须是 429。
        assert statuses[:10] == [200] * 10, statuses
        assert statuses[10] == 429, statuses
        body = (await apiClient.post(
            f"/api/v1/ontology-discovery/snapshots?sourceId={sourceId}"
        )).json()
        assert body["success"] is False
    finally:
        limiter.enabled = False
        limiter.reset()


# ---------------------------------------------------------------- /diff 披露


def _fakeDiffResult(changes):
    """构造一个 `OntologyDiffResult` 替身（避免真跑编译/发布链）。"""

    from app.services.discovery_diff_service import OntologyDiffResult
    from app.services.semantic_diff_service import SemanticDiff

    return OntologyDiffResult(
        baselineReleaseId=7,
        baselineVersion="v1",
        baselineHash="abc123",
        baselineDomainCode=None,
        scope="GLOBAL",
        diff=SemanticDiff(changes=tuple(changes)),
    )


async def test_diff响应披露字段级覆盖范围(apiClient, monkeypatch):
    """MEDIUM-5：分区级披露不够 —— 「比了 properties」但比的是一张**手写**字段表。

    将来 manifest 契约新增字段而引擎漏比时，分区级披露会与事实不符
    ⇒ 必须随结果披露**字段级**覆盖范围。
    """

    from app.services.semantic_diff_service import SemanticChange

    async def _fakeCompute(session, *, domainCode=None):
        return _fakeDiffResult(
            [SemanticChange(kind="CLASS_ADDED", severity="ADDITIVE", section="classes", target="订单")]
        )

    monkeypatch.setattr(ontology_discovery, "computeOntologyDiff", _fakeCompute)

    response = await apiClient.get("/api/v1/ontology-discovery/diff")

    assert response.status_code == 200, response.text
    payload = response.json()
    coveredFields = payload["coveredFields"]
    assert set(coveredFields) == {
        "classes",
        "properties",
        "joins",
        "sourceBindings",
    }, coveredFields
    assert coveredFields["classes"], "classes 分区必须披露比了哪些字段"
    assert coveredFields["sourceBindings"], "sourceBindings 分区必须披露比了哪些字段"
    # 引擎只比 classes/properties/joins/sourceBindings ⇒ 未覆盖分区非空
    # ⇒ partial 恒为 true
    # ⇒「无破坏性变更」只在已覆盖分区内成立（必须随结果披露）。
    assert payload["partial"] is True
    assert payload["uncoveredSections"], "未覆盖分区必须一并披露"
    assert payload["coveredSections"], "已覆盖分区必须一并披露"


async def test_diff响应如实报告截断与丢弃的破坏性条数(apiClient, monkeypatch):
    """MEDIUM-2 + 截断计数：调用方要能判断「破坏性变更有没有看全」。"""

    from app.services.semantic_diff_service import SemanticChange

    # 501 条破坏性 + 1 条增量：上限 500 ⇒ 截断，且丢了 1 条破坏性变更。
    changes = [
        SemanticChange(
            kind="CLASS_REMOVED", severity="BREAKING", section="classes", target=f"B{i}"
        )
        for i in range(501)
    ] + [
        SemanticChange(
            kind="CLASS_ADDED", severity="ADDITIVE", section="classes", target="A1"
        )
    ]

    async def _fakeCompute(session, *, domainCode=None):
        return _fakeDiffResult(changes)

    monkeypatch.setattr(ontology_discovery, "computeOntologyDiff", _fakeCompute)

    response = await apiClient.get("/api/v1/ontology-discovery/diff")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["truncated"] is True
    assert payload["totalChangeCount"] == 502
    assert payload["changeCount"] == 500
    assert payload["droppedBreakingCount"] == 1
    assert payload["breakingCount"] == 501
    assert payload["hasBreakingChanges"] is True
    # 展示出来的 500 条必须**全是破坏性变更**（破坏性优先）。
    severities = {change["severity"] for change in payload["changes"]}
    assert severities == {"BREAKING"}, severities


# =============================================================================
# G3-4：运行 / 候选 / 问题 / 提案 / 审核
# =============================================================================


async def _seedA2(session, *, suffix: str = "") -> dict[str, int]:
    """种源 + 快照 + 一条成功运行 + 候选 + 问题。"""
    from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
    from app.domain.discovery_pipeline_models import (
        DiscoveryCandidate,
        DiscoveryFinding,
        DiscoveryRun,
    )

    ds = await _seedDatasource(session, name=f"A2演示源{suffix}", type="oracle")
    source = DiscoverySource(
        name=f"A2源{suffix}",
        datasource_id=ds.id,
        owner="data-owner",
        access_policy={"mode": "READ_ONLY"},
    )
    session.add(source)
    await session.flush()
    snapshot = MetadataSnapshot(
        source_id=source.id,
        schema_hash=hashlib.sha256(suffix.encode()).hexdigest(),
        snapshot_json={"schemaVersion": "v2", "tables": []},
        table_count=0,
        column_count=0,
    )
    session.add(snapshot)
    await session.flush()
    run = DiscoveryRun(
        source_id=source.id,
        snapshot_id=snapshot.id,
        trigger="MANUAL",
        status="SUCCEEDED",
        prompt_tokens=100,
        completion_tokens=20,
        cost=0.00042,
        model_version="fake-model",
        prompt_version="v1",
    )
    session.add(run)
    await session.flush()
    session.add_all(
        [
            DiscoveryCandidate(
                run_id=run.id,
                source_id=source.id,
                snapshot_id=snapshot.id,
                candidate_type="OBJECT",
                table_name="DIM_SUPPLIER",
                target_code="SUPPLIER",
                payload={"className": "Supplier"},
                evidence_refs=[{"table": "DIM_SUPPLIER", "column": "BPSNUM_0"}],
                status="CANDIDATE",
            ),
            DiscoveryCandidate(
                run_id=run.id,
                source_id=source.id,
                snapshot_id=snapshot.id,
                candidate_type="PROPERTY",
                table_name="DIM_SUPPLIER",
                column_name="BPSNAM_0",
                payload={"propertyName": "supplier_name"},
                evidence_refs=[{"table": "DIM_SUPPLIER", "column": "BPSNAM_0"}],
                status="CANDIDATE",
            ),
            DiscoveryFinding(
                run_id=run.id,
                source_id=source.id,
                snapshot_id=snapshot.id,
                finding_type="MISSING",
                table_name="DIM_SUPPLIER",
                column_name="BPSNAM_0",
                severity="WARNING",
                detail="名称列有空值",
            ),
        ]
    )
    await session.commit()
    return {"sourceId": source.id, "snapshotId": snapshot.id, "runId": run.id}


async def test_受理运行返回202并落running行(apiClient, dbSession, monkeypatch):
    from app.api.v1 import ontology_discovery as apiModule

    seeded = await _seedA2(dbSession)
    calls: list[tuple] = []

    async def _fakePipeline(runId, **kwargs):
        calls.append((runId, kwargs))

    monkeypatch.setattr(apiModule._runService, "runPipeline", _fakePipeline)

    resp = await apiClient.post(
        "/api/v1/ontology-discovery/runs",
        json={"sourceId": seeded["sourceId"], "tables": ["DIM_SUPPLIER"]},
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "RUNNING"
    assert body["snapshotId"] == seeded["snapshotId"]
    assert calls and calls[0][1]["tables"] == ["DIM_SUPPLIER"]


async def test_受理运行拒绝未知字段(apiClient, dbSession):
    seeded = await _seedA2(dbSession)

    resp = await apiClient.post(
        "/api/v1/ontology-discovery/runs",
        json={"sourceId": seeded["sourceId"], "oops": 1},
    )

    assert resp.status_code == 422


async def test_运行详情可查(apiClient, dbSession):
    seeded = await _seedA2(dbSession)

    resp = await apiClient.get(f"/api/v1/ontology-discovery/runs/{seeded['runId']}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["promptTokens"] == 100
    assert body["completionTokens"] == 20
    assert body["modelVersion"] == "fake-model"


async def test_候选列表按run过滤且total是全量(apiClient, dbSession):
    seeded = await _seedA2(dbSession)

    resp = await apiClient.get(
        "/api/v1/ontology-discovery/candidates",
        params={"runId": seeded["runId"], "limit": 1},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["rows"]) == 1
    assert body["total"] == 2


async def test_问题列表返回证据为空数组(apiClient, dbSession):
    seeded = await _seedA2(dbSession)

    resp = await apiClient.get(
        "/api/v1/ontology-discovery/findings",
        params={"runId": seeded["runId"]},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["rows"][0]["evidenceRefs"] == []


async def test_提案创建返回201且状态DRAFT(apiClient, dbSession):
    from app.tests._knowledge_manifest import sampleManifest

    seeded = await _seedA2(dbSession)

    created = await apiClient.post(
        "/api/v1/ontology-discovery/proposals",
        json={
            "sourceId": seeded["sourceId"],
            "title": "供应商主数据修订",
            "manifest": sampleManifest(),
        },
    )
    assert created.status_code == 201, created.text
    proposal = created.json()
    assert proposal["status"] == "DRAFT"
    assert proposal["diffJson"]["kind"] == "MANIFEST_REPLACEMENT"


async def test_提案提交审核全流程(apiClient, dbSession):
    """提案人 ≠ 审核人（stub 身份的 userId 由 API 提供）⇒ 用服务以 alice 建提案。"""
    from app.services.discovery_proposal_service import DiscoveryProposalService
    from app.tests._knowledge_manifest import sampleManifest

    seeded = await _seedA2(dbSession)
    created = await DiscoveryProposalService().createProposal(
        dbSession,
        sourceId=seeded["sourceId"],
        title="供应商主数据修订",
        proposalManifest=sampleManifest(),
        proposer="alice",
    )
    proposal = {"id": created.id}

    submitted = await apiClient.post(
        f"/api/v1/ontology-discovery/proposals/{proposal['id']}/submit"
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "IN_REVIEW"

    reviewed = await apiClient.post(
        f"/api/v1/ontology-discovery/proposals/{proposal['id']}/review",
        json={"decision": "APPROVE", "comment": "同意"},
    )
    assert reviewed.status_code == 201, reviewed.text
    assert reviewed.json()["decision"] == "APPROVE"

    detail = await apiClient.get(
        f"/api/v1/ontology-discovery/proposals/{proposal['id']}"
    )
    assert detail.json()["status"] == "APPROVED"


async def test_提案列表可按状态过滤(apiClient, dbSession):
    from app.services.discovery_proposal_service import DiscoveryProposalService
    from app.tests._knowledge_manifest import sampleManifest

    seeded = await _seedA2(dbSession)
    service = DiscoveryProposalService()
    await service.createProposal(
        dbSession,
        sourceId=seeded["sourceId"],
        title="草稿提案",
        proposalManifest=sampleManifest(),
        proposer="alice",
    )

    resp = await apiClient.get(
        "/api/v1/ontology-discovery/proposals", params={"status": "DRAFT"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 1
    assert resp.json()["rows"][0]["proposer"] == "alice"


def _publishableManifest() -> dict:
    """能过发布闸门的样本（补齐 RATIO 落点与源类引用）。"""
    import copy

    from app.tests._knowledge_manifest import sampleManifest

    manifest = copy.deepcopy(sampleManifest())
    landings = {
        "supplier_delivery_days": ("Supplier", None),
        "po_amount": ("PurchaseOrder", None),
        "iqc_pass_rate": ("InspectionRecord", "PurchaseOrder"),
    }
    for metric in manifest["metrics"]:
        numeratorClass, denominatorClass = landings[metric["metricName"]]
        metric["numeratorMeasure"] = {
            "agg": "SUM", "expr": None, "filter": None,
            "className": numeratorClass, "classAlias": None,
        }
        metric["denominatorMeasure"] = (
            {"agg": "SUM", "expr": None, "filter": None,
             "className": denominatorClass, "classAlias": None}
            if denominatorClass else None
        )
        metric["sources"] = [{"className": numeratorClass, "role": "BASE"}]
    return manifest


async def test_提案发布走唯一写口并回填release(apiClient, dbSession):
    """G4：提交 → 审核 → 发布（DRAFT→VALIDATED→ACTIVE），提案置 PUBLISHED。"""
    from app.services.discovery_proposal_service import DiscoveryProposalService

    seeded = await _seedA2(dbSession)
    proposal = await DiscoveryProposalService().createProposal(
        dbSession,
        sourceId=seeded["sourceId"],
        title="发布提案",
        proposalManifest=_publishableManifest(),
        proposer="alice",
    )
    await apiClient.post(f"/api/v1/ontology-discovery/proposals/{proposal.id}/submit")
    await apiClient.post(
        f"/api/v1/ontology-discovery/proposals/{proposal.id}/review",
        json={"decision": "APPROVE"},
    )

    resp = await apiClient.post(
        f"/api/v1/ontology-discovery/proposals/{proposal.id}/publish",
        json={"version": "2026.11.22-publish1"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["proposalStatus"] == "PUBLISHED"
    assert body["status"] == "ACTIVE"
    assert body["releaseId"] > 0


async def test_增量监测返回changed并可选起运行(apiClient, dbSession, monkeypatch):
    from app.api.v1 import ontology_discovery as apiModule
    from app.services.discovery_monitor_service import MonitorResult

    seeded = await _seedA2(dbSession)
    calls: list[int] = []

    async def _fakeCheck(session, *, sourceId, createdBy=None):
        return MonitorResult(sourceId=sourceId, snapshotId=999, changed=True, runId=999)

    async def _fakePipeline(runId, **kwargs):
        calls.append(runId)

    monkeypatch.setattr(apiModule._monitorService, "checkSource", _fakeCheck)
    monkeypatch.setattr(apiModule._runService, "runPipeline", _fakePipeline)

    resp = await apiClient.post(
        f"/api/v1/ontology-discovery/sources/{seeded['sourceId']}/monitor"
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is True
    assert body["runId"] == 999
    assert calls == [999]


async def test_评测运行返回可复算指标(apiClient, dbSession):
    seeded = await _seedA2(dbSession)

    resp = await apiClient.post(
        f"/api/v1/ontology-discovery/runs/{seeded['runId']}/evaluate",
        json={"datasetVersion": "pilot-2026-11"},
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["datasetVersion"] == "pilot-2026-11"
    assert body["metricsJson"]["candidateCount"] == 2
    # _seedA2 没种剖析行 ⇒ 证据解析不了 ⇒ FAIL（有候选但证据不实）
    assert body["verdict"] == "FAIL"

    listed = await apiClient.get("/api/v1/ontology-discovery/evaluations")
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] >= 1
