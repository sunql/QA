"""Task 7 集成测试：编排运行的 create 路径（真实 PostgreSQL + 完整 API 链路）。

覆盖：
- 必须带 modelConfigId、模型必须存在且 active、快照 instructions/bindings/schema/预算、
  返回 202 QUEUED、计数归零、cost_source=UNKNOWN
- create 失败路径（缺/禁用 profile、Agent 非 ACTIVE、无有效绑定、超系统预算）→ 409/422 且零行

共享的常量 / client 夹具 / 造数助手在 `_agent_runs_api_helpers.py`。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests.integration._agent_runs_api_helpers import (
    _AGENT_CODE,
    _BASE_PATH,
    _TOOL_NAME,
    _VALID_SCHEMA,
    OWNER_HEADERS,
    _countRuns,
    _createAgent,
    _createModel,
    _createProfile,
    _createRun,
    _createTool,
    _disableTool,
)
from app.tests.integration._agent_runs_api_helpers import client as client

# ---------------------------------------------------------------------------
# create 造数 / 断言助手
# ---------------------------------------------------------------------------


async def _seedCreateScenario(client: AsyncClient) -> int:
    """Agent + 工具 + profile + 模型全部就绪，返回 modelConfigId。"""
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    return await _createModel(client)


def _assertQueuedRunIdentity(run: dict, runId: str, modelId: int) -> None:
    assert run["runId"] == runId
    assert run["agentCode"] == _AGENT_CODE
    assert run["createdBy"] == "orch-owner"
    assert run["modelConfigId"] == modelId
    assert run["status"] == "QUEUED"


def _assertQueuedRunSnapshot(run: dict) -> None:
    assert run["instructionsSnapshot"] == "Answer using the bound read-only tools."
    assert run["toolBindingsSnapshot"] == [
        {
            "toolName": _TOOL_NAME,
            "description": f"{_TOOL_NAME} description",
            "inputSchema": _VALID_SCHEMA,
            "sortOrder": 0,
        }
    ]


def _assertQueuedRunCounters(run: dict) -> None:
    assert run["maxIterations"] == 6
    assert run["maxToolCalls"] == 8
    assert run["maxCostUsd"] == 1.0
    assert run["timeoutSeconds"] == 120
    assert run["iterationCount"] == 0
    assert run["toolCallCount"] == 0
    assert run["totalTokens"] == 0
    assert run["cost"] == 0.0
    assert run["costSource"] == "UNKNOWN"
    assert run["costComplete"] is False


def _assertQueuedRunLifecycle(run: dict) -> None:
    assert run["pauseCode"] is None
    assert run["cancelRequested"] is False
    assert run["revision"] == 1
    assert run["startedAt"] is None
    assert run["finishedAt"] is None


def _assertSnapshotHidesToolInternals(run: dict) -> None:
    # 快照内不得出现工具配置内部字段
    snapshotText = str(run["toolBindingsSnapshot"])
    assert "handlerRef" not in snapshotText
    assert "handler_ref" not in snapshotText
    assert "wiki_search" not in snapshotText


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_requires_model_config_id(client: AsyncClient) -> None:
    resp = await client.post(
        _BASE_PATH,
        headers=OWNER_HEADERS,
        json={"agentCode": _AGENT_CODE, "input": "hi"},
    )
    assert resp.status_code == 422, resp.text


async def test_create_returns_202_queued_with_snapshots(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    modelId = await _seedCreateScenario(client)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "QUEUED"
    runId = body["runId"]
    assert isinstance(runId, str) and len(runId) == 36

    detail = await client.get(f"{_BASE_PATH}/{runId}", headers=OWNER_HEADERS)
    assert detail.status_code == 200, detail.text
    run = detail.json()
    _assertQueuedRunIdentity(run, runId, modelId)
    _assertQueuedRunSnapshot(run)
    _assertQueuedRunCounters(run)
    _assertQueuedRunLifecycle(run)
    _assertSnapshotHidesToolInternals(run)
    # P3c D26/D27：无激活发布 ⇒ 钉住三列全 NULL（旧 run 语义不变）
    assert run["semanticReleaseId"] is None
    assert run["semanticReleaseVersion"] is None
    assert run["semanticManifestHash"] is None
    assert await _countRuns(dbSession) == 1


async def test_create_pins_the_current_active_semantic_release(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """P3c D27：创建时把当前 ACTIVE 发布冻结进 run（重放一致的前提）。

    发布行**直接落库**（本文件唯一的直接写 semantic_release 的用例）；
    Registry 有 TTL 缓存 ⇒ 插行后必须 invalidate。
    """
    import hashlib

    from app.domain.semantic_manifest_schema import canonicalManifestJson
    from app.domain.semantic_release_models import SemanticRelease
    from app.services.semantic_registry_service import semanticRegistry

    manifest = {
        "classes": [
            {
                "className": "采购订单",
                "classAlias": "PO",
                "description": "采购订单",
                "sourceTable": "po",
                "parentClassName": None,
                "properties": [],
            }
        ],
        "joins": [],
        "metrics": [],
        "kpis": [],
    }
    # 哈希口径必须与 Registry 的规范化一致（canonicalManifestJson：键序稳定、无空白）
    payload = canonicalManifestJson(manifest)
    dbSession.add(
        SemanticRelease(
            domain_code=None,
            version="2026.11.15-1",
            status="ACTIVE",
            manifest_json=manifest,
            manifest_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            created_by="pin-test",
        )
    )
    # 必须 commit：createRun 走 HTTP（独立连接/事务）⇒ 未提交的行对它不可见
    await dbSession.commit()
    semanticRegistry.invalidate()

    modelId = await _seedCreateScenario(client)
    resp = await _createRun(client, modelConfigId=modelId)
    assert resp.status_code == 202, resp.text
    runId = resp.json()["runId"]

    detail = await client.get(f"{_BASE_PATH}/{runId}", headers=OWNER_HEADERS)
    assert detail.status_code == 200, detail.text
    run = detail.json()
    assert run["semanticReleaseId"] is not None
    assert run["semanticReleaseVersion"] == "2026.11.15-1"
    assert run["semanticManifestHash"] == hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # 运行期发布变更不影响该 run：改成 ARCHIVED 后 detail 的钉住快照原样
    release = (
        (await dbSession.execute(select(SemanticRelease)))
        .scalars()
        .all()
    )[-1]
    release.status = "ARCHIVED"
    await dbSession.flush()
    semanticRegistry.invalidate()
    detail2 = await client.get(f"{_BASE_PATH}/{runId}", headers=OWNER_HEADERS)
    assert detail2.status_code == 200
    run2 = detail2.json()
    assert run2["semanticReleaseId"] == run["semanticReleaseId"]
    assert run2["semanticReleaseVersion"] == "2026.11.15-1"


async def test_create_merges_provided_limits_over_profile_defaults(
    client: AsyncClient,
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)

    resp = await _createRun(
        client,
        modelConfigId=modelId,
        limits={"maxIterations": 3, "timeoutSeconds": 45},
    )
    assert resp.status_code == 202, resp.text

    detail = await client.get(f"{_BASE_PATH}/{resp.json()['runId']}", headers=OWNER_HEADERS)
    run = detail.json()
    assert run["maxIterations"] == 3
    assert run["timeoutSeconds"] == 45
    # 未覆盖的字段回落 profile 默认值
    assert run["maxToolCalls"] == 8
    assert run["maxCostUsd"] == 1.0


async def test_create_with_unknown_model_returns_404(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)

    resp = await _createRun(client, modelConfigId=987654)

    assert resp.status_code == 404, resp.text
    # 领域 404（而非路由不存在）
    assert "error" in resp.json()
    assert resp.json()["error"] != "Not Found"
    assert await _countRuns(dbSession) == 0


async def test_create_with_inactive_model_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client, "orch-run-inactive-model", isActive=False)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 409, resp.text
    assert await _countRuns(dbSession) == 0


async def test_create_without_profile_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    modelId = await _createModel(client)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 409, resp.text
    assert await _countRuns(dbSession) == 0


async def test_create_with_disabled_profile_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client, enabled=False)
    modelId = await _createModel(client)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 409, resp.text
    assert await _countRuns(dbSession) == 0


async def test_create_with_non_active_agent_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client, status="draft")
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 409, resp.text
    assert await _countRuns(dbSession) == 0


async def test_create_without_valid_binding_returns_409(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)
    # 工具被禁用后，绑定不再「当前有效」
    await _disableTool(client)

    resp = await _createRun(client, modelConfigId=modelId)

    assert resp.status_code == 409, resp.text
    assert await _countRuns(dbSession) == 0


async def test_create_with_limits_over_system_ceiling_returns_422(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await _createAgent(client)
    await _createTool(client)
    await _createProfile(client)
    modelId = await _createModel(client)

    resp = await _createRun(client, modelConfigId=modelId, limits={"maxIterations": 9999})

    assert resp.status_code == 422, resp.text
    assert await _countRuns(dbSession) == 0
