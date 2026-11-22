"""发现候选生成服务（G3-2）：真实 PG + 假 LLM 客户端。

钉住六件事：

1. **证据完整率 100% 才落库**：没有证据的候选**不写**（不猜、不补证据）；
2. **计量必写**：token 与 cost 累加到 `discovery_run`，`model_version` /
   `prompt_version` 留档 —— 即便回复解析失败，**已花的钱也要记账**；
3. **幂等**：同一个 run 重跑候选 ⇒ 覆盖，不追加两套；
4. **前置条件**：run 必须 SUCCEEDED、必须有剖析行、必须能解析出模型配置；
5. **只写发现域**：候选/问题落 `discovery_candidate` / `discovery_finding`；
6. **失败不污染 run**：候选步骤失败不改 run 状态（剖析本身是成功的）。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
from app.domain.discovery_pipeline_models import (
    DiscoveryCandidate,
    DiscoveryFinding,
    DiscoveryProfile,
    DiscoveryRun,
)
from app.domain.exceptions import NoAvailableModelError, ValidationError
from app.domain.models import DataSource, LlmConfig
from app.infrastructure.llm.base_client import LlmMessage, LlmResponse
from app.infrastructure.security.crypto import encryptApiKey
from app.services.discovery_candidate_service import DiscoveryCandidateService

_SNAPSHOT: dict[str, Any] = {
    "schemaVersion": "v2",
    "tables": [
        {
            "tableName": "DIM_SUPPLIER",
            "owner": "THBI",
            "columns": [
                {"columnName": "BPSNUM_0", "dataType": "VARCHAR2(20)"},
                {"columnName": "BPSNAM_0", "dataType": "VARCHAR2(200)"},
            ],
        }
    ],
}

_GOOD_RESPONSE: dict[str, Any] = {
    "candidates": [
        {
            "candidateType": "OBJECT",
            "table": "DIM_SUPPLIER",
            "column": None,
            "targetCode": "SUPPLIER",
            "confidence": 0.8,
            "payload": {"className": "Supplier", "objectType": "Master"},
            "evidence": [{"table": "DIM_SUPPLIER", "column": "BPSNUM_0"}],
        },
        {
            "candidateType": "PROPERTY",
            "table": "DIM_SUPPLIER",
            "column": "BPSNAM_0",
            "targetCode": "SUPPLIER.supplier_name",
            "confidence": 0.6,
            "payload": {"propertyName": "supplier_name"},
            "evidence": [{"table": "DIM_SUPPLIER", "column": "BPSNAM_0"}],
        },
    ],
    "findings": [
        {
            "findingType": "MISSING",
            "table": "DIM_SUPPLIER",
            "column": "BPSNAM_0",
            "severity": "WARNING",
            "detail": "名称列存在空值",
            "evidence": [{"table": "DIM_SUPPLIER", "column": "BPSNAM_0"}],
        }
    ],
}


class _FakeLlm:
    def __init__(self, content: str, *, promptTokens: int = 100, completionTokens: int = 20):
        self.content = content
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens
        self.calls: list[list[LlmMessage]] = []

    async def complete(self, messages: list[LlmMessage], **kwargs: Any) -> LlmResponse:
        self.calls.append(messages)
        return LlmResponse(
            content=self.content,
            modelName="fake-model",
            promptTokens=self.promptTokens,
            completionTokens=self.completionTokens,
            totalTokens=self.promptTokens + self.completionTokens,
        )


async def _seed(
    session: Any, *, status: str = "SUCCEEDED", withProfiles: bool = True
) -> tuple[int, int]:
    datasource = DataSource(
        name=f"LLM_SRC_{status}_{withProfiles}",
        type="oracle",
        host="h",
        port=1521,
        database_name="THBI",
        username="THBI",
        password_encrypted=encryptApiKey("secret"),
        is_active=True,
    )
    session.add(datasource)
    session.add(
        LlmConfig(
            model_name="fake-model",
            provider="openai",
            cost_per_1k_input=Decimal("0.003"),
            cost_per_1k_output=Decimal("0.006"),
            is_active=True,
        )
    )
    await session.flush()
    source = DiscoverySource(
        name=f"候选源{status}{withProfiles}",
        datasource_id=datasource.id,
        owner="data-owner",
        access_policy={"mode": "READ_ONLY"},
    )
    session.add(source)
    await session.flush()
    snapshot = MetadataSnapshot(
        source_id=source.id,
        schema_hash="b" * 64,
        snapshot_json=_SNAPSHOT,
        table_count=1,
        column_count=2,
    )
    session.add(snapshot)
    await session.flush()
    run = DiscoveryRun(
        source_id=source.id,
        snapshot_id=snapshot.id,
        trigger="MANUAL",
        status=status,
    )
    session.add(run)
    await session.flush()
    if withProfiles:
        session.add_all(
            [
                DiscoveryProfile(
                    run_id=run.id,
                    source_id=source.id,
                    snapshot_id=snapshot.id,
                    table_name="DIM_SUPPLIER",
                    column_name=None,
                    row_count=3500,
                    sql_digest="c" * 64,
                ),
                DiscoveryProfile(
                    run_id=run.id,
                    source_id=source.id,
                    snapshot_id=snapshot.id,
                    table_name="DIM_SUPPLIER",
                    column_name="BPSNUM_0",
                    row_count=3500,
                    null_count=0,
                    distinct_count=3500,
                    sql_digest="c" * 64,
                ),
                DiscoveryProfile(
                    run_id=run.id,
                    source_id=source.id,
                    snapshot_id=snapshot.id,
                    table_name="DIM_SUPPLIER",
                    column_name="BPSNAM_0",
                    row_count=3500,
                    null_count=3,
                    distinct_count=3436,
                    sql_digest="c" * 64,
                ),
            ]
        )
    await session.commit()
    return run.id, source.id


async def _candidates(session: Any, runId: int) -> list[DiscoveryCandidate]:
    return list(
        (
            await session.execute(
                select(DiscoveryCandidate).where(DiscoveryCandidate.run_id == runId)
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


async def test_generates_candidates_findings_and_records_metering(dbSession: Any) -> None:
    runId, _ = await _seed(dbSession)
    client = _FakeLlm(json.dumps(_GOOD_RESPONSE, ensure_ascii=False))

    result = await DiscoveryCandidateService().generateCandidates(
        dbSession, runId=runId, llmClient=client
    )

    assert (result.candidateCount, result.findingCount) == (2, 1)
    assert result.skipped == ()
    candidates = await _candidates(dbSession, runId)
    assert {row.candidate_type for row in candidates} == {"OBJECT", "PROPERTY"}
    assert all(row.evidence_refs for row in candidates)
    assert all(row.status == "CANDIDATE" for row in candidates)
    assert all(row.model_version == "fake-model" for row in candidates)
    assert all(row.prompt_version for row in candidates)
    findings = list(
        (
            await dbSession.execute(
                select(DiscoveryFinding).where(DiscoveryFinding.run_id == runId)
            )
        )
        .scalars()
        .all()
    )
    assert len(findings) == 1
    assert findings[0].status == "OPEN"

    run = await dbSession.get(DiscoveryRun, runId)
    assert run.prompt_tokens == 100
    assert run.completion_tokens == 20
    # 100/1000*0.003 + 20/1000*0.006 = 0.0003 + 0.00012
    assert Decimal(str(run.cost)) == Decimal("0.000420")
    assert run.model_version == "fake-model"


async def test_prompt_wraps_profile_facts(dbSession: Any) -> None:
    runId, _ = await _seed(dbSession)
    client = _FakeLlm(json.dumps(_GOOD_RESPONSE, ensure_ascii=False))

    await DiscoveryCandidateService().generateCandidates(
        dbSession, runId=runId, llmClient=client
    )

    user = client.calls[0][-1].content
    assert "<discovery_profile>" in user
    assert "DIM_SUPPLIER" in user


async def test_regeneration_replaces_previous_rows(dbSession: Any) -> None:
    runId, _ = await _seed(dbSession)
    content = json.dumps(_GOOD_RESPONSE, ensure_ascii=False)
    service = DiscoveryCandidateService()

    await service.generateCandidates(dbSession, runId=runId, llmClient=_FakeLlm(content))
    await service.generateCandidates(dbSession, runId=runId, llmClient=_FakeLlm(content))

    assert len(await _candidates(dbSession, runId)) == 2
    run = await dbSession.get(DiscoveryRun, runId)
    assert run.prompt_tokens == 200


# ---------------------------------------------------------------------------
# 坏条目 / 前置条件
# ---------------------------------------------------------------------------


async def test_candidate_without_evidence_is_not_persisted(dbSession: Any) -> None:
    runId, _ = await _seed(dbSession)
    body = json.loads(json.dumps(_GOOD_RESPONSE))
    body["candidates"][1]["evidence"] = []

    result = await DiscoveryCandidateService().generateCandidates(
        dbSession, runId=runId, llmClient=_FakeLlm(json.dumps(body, ensure_ascii=False))
    )

    assert result.candidateCount == 1
    assert len(result.skipped) == 1
    assert len(await _candidates(dbSession, runId)) == 1


async def test_broken_json_records_cost_then_raises(dbSession: Any) -> None:
    """钱已经花了：解析失败也必须把 token/cost 记到 run 上。"""
    runId, _ = await _seed(dbSession)

    with pytest.raises(ValidationError):
        await DiscoveryCandidateService().generateCandidates(
            dbSession, runId=runId, llmClient=_FakeLlm("这不是 JSON")
        )

    run = await dbSession.get(DiscoveryRun, runId)
    assert run.prompt_tokens == 100
    assert await _candidates(dbSession, runId) == []


async def test_run_must_be_succeeded(dbSession: Any) -> None:
    runId, _ = await _seed(dbSession, status="RUNNING")
    client = _FakeLlm(json.dumps(_GOOD_RESPONSE, ensure_ascii=False))

    with pytest.raises(ValidationError):
        await DiscoveryCandidateService().generateCandidates(
            dbSession, runId=runId, llmClient=client
        )

    assert client.calls == []


async def test_run_must_have_profiles(dbSession: Any) -> None:
    runId, _ = await _seed(dbSession, withProfiles=False)
    client = _FakeLlm(json.dumps(_GOOD_RESPONSE, ensure_ascii=False))

    with pytest.raises(ValidationError):
        await DiscoveryCandidateService().generateCandidates(
            dbSession, runId=runId, llmClient=client
        )

    assert client.calls == []


async def test_missing_model_config_raises(dbSession: Any) -> None:
    runId, _ = await _seed(dbSession)
    await dbSession.execute(
        select(LlmConfig).where(LlmConfig.model_name == "fake-model")
    )
    # 停用唯一配置 ⇒ 无可解析模型
    config = (
        await dbSession.execute(select(LlmConfig))
    ).scalars().first()
    config.is_active = False
    await dbSession.commit()
    client = _FakeLlm(json.dumps(_GOOD_RESPONSE, ensure_ascii=False))

    with pytest.raises(NoAvailableModelError):
        await DiscoveryCandidateService().generateCandidates(
            dbSession, runId=runId, llmClient=client
        )

    assert client.calls == []
