"""Task 9（重整形）：环节一证据契约的 **HTTP 端到端**验收（真实 PG + 完整 HTTP 链路）。

承重集成点：``GET /research/sessions/{id}/report`` 返回 ``payload["evidence"]["stepResults"]``。
Task 8 的报告证据此前只在 ``ReportPlanner.compose`` 层被断言
（``research/test_step_result_by_turn.py:185``），**从未**经报告端点走一遍；而
``test_research_api.py`` 的 ``/report`` 用例用的是合成 ``{"v": 1}`` payload，从不跑 turn。
本文件把整条链路在真 HTTP 上串起来验收：
``POST /research/sessions`` → ``POST /research/sessions/{id}/turns``（202）→
``GET /research/sessions/{id}/report``（200）。

驱动方式：复用 ``test_research_stream.py:_fullService``（autoConfirm 全跑管线 + 真实
``ReportPlanner`` ⇒ 跑到报告相位 ``publishReport``；每步经真实 ``_stageExecute`` 落
``research_step_result``）。**不用** ``test_research_api.py:_fastService`` —— 它的
``_EmptyEsl`` 卡在 checkpoint #1，永远到不了报告相位。httpx ``ASGITransport`` 下
BackgroundTasks 于 ``client.post`` 返回前跑完 ⇒ 无需轮询 / SSE。

五个场景的既有覆盖（本文件**不**重复 a/b/c 的既有 HTTP/SSE 覆盖）：
- (a) 追问不回落：已有 HTTP 覆盖（``research/test_run_state_recovery.py:183``）；
- (b) 并发拒绝：已有 HTTP 覆盖（``research/test_run_concurrency.py:306``）；
- (c) Decimal/date 序列化：已有 **HTTP-SSE** 覆盖（``research/test_sse_serialization.py:135``，
  另有 stage 层 ``research/test_step_result_snapshot.py:213``）；**本文件新增**其在
  **报告端点**层的覆盖（``summary`` 数值标量端到端是 JSON 字符串）；
- (d) 零支持不标 verified：此前 stage 层（``research/test_hypothesis_verdict.py:171/187``）
  + **报告 compose 层**（``:290`` 已断 ``payload["findingsRef"][0]["verified"]``）覆盖 ⇒
  **本文件新增 HTTP 报告端点层**（端到端经 ``GET /report`` 序列化）；
- (e) 步结果进报告 ``payload.evidence``：此前仅 planner 层覆盖 ⇒ **本文件新增 HTTP 端点层**。
  注：本文件的 turn 只跑**单个** run_state 版本（1 步），故其行数交叉校验不覆盖**跨版本**
  聚合 —— 跨版本（2 版本 / 6 步）覆盖在 ``research/test_step_result_by_turn.py:185``。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.enums import DataSourceType
from app.domain.models import DataSource
from app.domain.research_models import ResearchFinding, ResearchRunState, ResearchStepResult
from app.infrastructure.security.crypto import encryptApiKey
from app.models.rbac import User
from app.tests.integration.test_research_stream import _fullService

pytestmark = pytest.mark.asyncio

_BASE = "/api/v1/research"
_QUESTION = "供应商收货量为什么下降"

# 报告 evidence 条目契约字段集（Task 8 D25）：**不含** ``rows`` / ``raw_result_ref``（体积）。
_EVIDENCE_ITEM_FIELDS = frozenset(
    {"index", "type", "sql", "status", "error", "summary", "schemaVersion"}
)
# ``EvidenceSummary`` 的数值标量字段（线上 camelCase 别名，``model_dump(by_alias=True)`` 落库）。
# 这些字段在线上的契约是**字符串**（``research_json`` 的 ``Decimal -> str`` 口径，D14）。
_NUMERIC_SUMMARY_FIELDS = ("total", "baseline", "current", "delta", "deltaRatio")
# ``verified`` 为真的**唯一** verdict（``report_planner._FindingView.verified``）。
_VERIFIED_VERDICT = "supported"


# ---------------------------------------------------------------------------
# 夹具（镜子 ``test_research_api.py`` / ``test_research_stream.py``：research 专属夹具按文件定义）
# ---------------------------------------------------------------------------


async def _seedDatasource(
    dbSession: AsyncSession, name: str = "evidence-e2e-oracle", *, isDefault: bool = True
) -> DataSource:
    """种一个**启用的业务数据源**（建会话必须能解析到业务源）。

    Oracle 形态与生产同口径；本文件的 runner 是 fake，适配器只被构造、从不建连，
    故 host 用不可达假名即可。
    """
    ds = DataSource(
        name=name,
        type=DataSourceType.ORACLE,
        host="oracle-test",
        port=1521,
        database_name="THBIDB",
        username="THBI",
        password_encrypted=encryptApiKey("test-password"),
        is_active=True,
        is_default=isDefault,
        oracle_version="19.0.0.0.0",
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


@pytest.fixture()
async def authHeaders(dbSession: AsyncSession) -> dict[str, str]:
    """用户 stub 头（DB 命中 ⇒ dbUserId 非空）+ 一个启用的默认业务源。"""
    dbSession.add(
        User(
            username="research-evidence-e2e",
            display_name="e2e",
            email=None,
            enabled=True,
            password_hash=None,
        )
    )
    await dbSession.commit()
    await _seedDatasource(dbSession)
    return {"X-User-Id": "research-evidence-e2e"}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


async def _runFullTurn(
    client: AsyncClient, headers: dict[str, str]
) -> tuple[str, uuid.UUID]:
    """建会话 + 驱动一次 turn 到终态（BackgroundTasks 在本协程内跑完）⇒ (sessionId, turnId)。"""
    created = await client.post(
        f"{_BASE}/sessions", json={"question": _QUESTION}, headers=headers
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]

    accepted = await client.post(
        f"{_BASE}/sessions/{sid}/turns", json={"question": _QUESTION}, headers=headers
    )
    assert accepted.status_code == 202, accepted.text
    return sid, uuid.UUID(accepted.json()["turnId"])


async def _stepResultsForTurn(
    dbSession: AsyncSession, turnId: uuid.UUID
) -> list[ResearchStepResult]:
    """按 turn **直查** ``research_step_result``（join ``research_run_state.turn_id``）。

    ``research_step_result`` 无 ``turn_id`` 列（挂 ``run_state_id``，见其类 docstring）⇒
    需 join ``research_run_state`` 覆盖该 turn 名下所有版本。本查询**独立于**报告用的
    ``loadStepResultsByTurn`` 读取器，故「报告内容 == DB 真值」是一条真交叉检验。
    """
    rows = await dbSession.execute(
        select(ResearchStepResult)
        .join(ResearchRunState, ResearchStepResult.run_state_id == ResearchRunState.id)
        .where(ResearchRunState.turn_id == turnId)
        .order_by(ResearchStepResult.step_index)
    )
    return list(rows.scalars().all())


async def _findingVerdict(dbSession: AsyncSession, turnId: uuid.UUID) -> str | None:
    """该 turn 的 finding 落库 ``supporting_data["verdict"]``（历史行无此键 ⇒ ``None``）。"""
    row = (
        await dbSession.execute(
            select(ResearchFinding).where(ResearchFinding.turn_id == turnId)
        )
    ).scalars().first()
    assert row is not None, "该 turn 应有 finding 落库"
    return (row.supporting_data or {}).get("verdict")


def _assertSummaryNumericsAreStrings(evidence: list[dict[str, Any]]) -> None:
    """(c) 报告端点层：``summary`` 的数值标量是 JSON **字符串**（不是 float / Decimal）。

    端到端口径：``EvidenceSummary`` 把数值归一为字符串（``research_json`` 的
    ``Decimal -> str``）⇒ 落 ``research_step_result.evidence_summary``（JSONB）⇒ 经
    ``GET /report`` 出来仍是 ``str``。若中途被 ``_coerceJsonSafe`` 之流转成 float，
    则该断言失败（本仓「两套 JSON 安全映射」的经典坑）。
    """
    asserted = 0
    for item in evidence:
        summary = item.get("summary") or {}
        for field in _NUMERIC_SUMMARY_FIELDS:
            value = summary.get(field)
            if value is None:
                continue
            assert isinstance(value, str), (
                f"summary.{field} 应为字符串，实际 {type(value).__name__}: {value!r}"
            )
            asserted += 1
    assert asserted, "没有可断言的数值标量（夹具未产出数值摘要 ⇒ 本断言形同虚设）"


# ---------------------------------------------------------------------------
# (e) 承重：GET /report 返回 evidence.stepResults（端到端，真实 HTTP）
# ---------------------------------------------------------------------------


async def test_report_endpoint_returns_evidence_step_results(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全链路跑到报告相位：``GET /report`` 的 ``payload.evidence.stepResults`` 与真落库一致。"""
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid, turnId = await _runFullTurn(client, authHeaders)

    resp = await client.get(f"{_BASE}/sessions/{sid}/report", headers=authHeaders)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["status"] == "published"
    payload = report["payload"]

    evidence = payload["evidence"]["stepResults"]
    assert evidence, "报告应携带非空 evidence.stepResults"

    # 字段集恰为契约集；**不含** rows / raw_result_ref（体积字段不进报告）
    for item in evidence:
        assert set(item) == _EVIDENCE_ITEM_FIELDS, f"证据条目字段集漂移: {set(item)}"
        assert "rows" not in item and "raw_result_ref" not in item

    # 与 DB 对齐：报告透传的是**真落库**数据（行数 + step_index 序列一致），不是重算
    dbRows = await _stepResultsForTurn(dbSession, turnId)
    assert len(evidence) == len(dbRows), (
        f"evidence 条数({len(evidence)}) 与 DB 行数({len(dbRows)}) 不一致"
    )
    assert [item["index"] for item in evidence] == [row.step_index for row in dbRows]

    # (c) 报告端点层：summary 数值标量端到端是字符串
    _assertSummaryNumericsAreStrings(evidence)


# ---------------------------------------------------------------------------
# (d) 可选场景：零支持 finding 经报告端点**不**标 verified
# ---------------------------------------------------------------------------


async def test_report_endpoint_marks_unsupported_finding_unverified(
    client: AsyncClient,
    dbSession: AsyncSession,
    authHeaders: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) 证据不足的 finding 经 ``GET /report`` **不**标 ``verified``（HTTP 端点层）。

    场景 (d) 此前只有 stage 层覆盖（``research/test_hypothesis_verdict.py``）。本用例把它提到
    HTTP 报告端点层，且**无需注入新 fake**：默认 ``_fullService`` 的 ``FakeRunner.runVerification``
    返回单行 ``{"cnt": 1}``（无时间轴）⇒ ``summarizeEvidence`` 得 ``baseline=None`` ⇒
    ``verdict=inconclusive``（证据不足，即「零支持」）⇒ ``verified`` 必须为 ``False``。

    反向守卫（防假绿）：DB 侧 verdict 必须是**非 supported** 的**真实**判决值，而不是「历史
    finding 无 verdict 键」的回落路径（后者 ``verified`` 会回落成 ``True``）。
    """
    monkeypatch.setattr(researchModule, "buildResearchAgentService", _fullService)
    sid, turnId = await _runFullTurn(client, authHeaders)

    resp = await client.get(f"{_BASE}/sessions/{sid}/report", headers=authHeaders)
    assert resp.status_code == 200, resp.text
    findingsRef = resp.json()["payload"]["findingsRef"]
    assert findingsRef, "报告应携带 findingsRef"

    assert findingsRef[0]["verified"] is False, "证据不足的 finding 不得标 verified"

    # 反向守卫：DB 侧 verdict 确是非 supported 的真实判决（不是缺键回落）
    verdict = await _findingVerdict(dbSession, turnId)
    assert verdict is not None, "finding 应带 verdict（否则是回落路径，断言无意义）"
    assert verdict != _VERIFIED_VERDICT, f"本场景应为非 supported，实际 verdict={verdict!r}"
