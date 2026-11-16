"""P4 D36：试点跑批的**判定逻辑**（纯函数）单测。

试点问题集的通过率判据（计划 §6）
------------------------------
每条问题的三项**同时**成立才算通过：
1. **权限结果**：期望可见 → 至少一路拿到东西；期望不可见 → 四路 `DENIED` 且零证据；
2. **证据命中**：期望的类名 / 指标名 / KPI 码 / JOIN 键都在证据里；
3. **引用可回溯**：每条证据都带 `releaseId` + `releaseVersion` + `source=RELEASE`。

本文件把这些判据做成**纯函数**（`pilot_eval.evaluateResponse`）⇒ 判定逻辑本身
可单测，脚本只负责取数与打印（判定不许藏在 I/O 里，否则没法单测）。

⚠️ 本文件在 `unit/` 下 ⇒ 允许新建（unit 分片按路径稳定哈希）。
"""

from __future__ import annotations

from typing import Any

from app.domain.knowledge_schemas import (
    KNOWLEDGE_CHANNEL_GRAPH,
    KNOWLEDGE_CHANNEL_METRIC,
    KNOWLEDGE_CHANNEL_RAG,
    KNOWLEDGE_CHANNEL_SQL,
    KNOWLEDGE_ROUTE_DEGRADED,
    KNOWLEDGE_ROUTE_DENIED,
    KNOWLEDGE_ROUTE_EMPTY,
    KNOWLEDGE_ROUTE_OK,
    KNOWLEDGE_SOURCE_LIVE,
    KNOWLEDGE_SOURCE_RELEASE,
    KnowledgeEvidenceRead,
    KnowledgeQueryResponse,
    KnowledgeReleaseRef,
    KnowledgeRouteResultRead,
)
from app.services.knowledge.pilot_eval import (
    PilotExpectation,
    PilotIdentity,
    PilotQuestion,
    evaluateResponse,
    parseQuestionSet,
)

_IDENTITY = PilotIdentity(roles=("user",), departments=("PROCUREMENT_ORG",))


def _question(
    *,
    visibility: str = "VISIBLE",
    channels: dict[str, str] | None = None,
    evidenceRefs: tuple[str, ...] = ("Supplier",),
    minEvidence: int = 1,
) -> PilotQuestion:
    return PilotQuestion(
        id="P-01",
        question="供应商交付天数",
        domain="PROCUREMENT",
        identity=_IDENTITY,
        expect=PilotExpectation(
            visibility=visibility,
            channels=(
                channels
                if channels is not None
                else {
                    KNOWLEDGE_CHANNEL_GRAPH: KNOWLEDGE_ROUTE_OK,
                    KNOWLEDGE_CHANNEL_METRIC: KNOWLEDGE_ROUTE_OK,
                    KNOWLEDGE_CHANNEL_RAG: KNOWLEDGE_ROUTE_DEGRADED,
                    KNOWLEDGE_CHANNEL_SQL: KNOWLEDGE_ROUTE_OK,
                }
            ),
            evidenceRefs=evidenceRefs,
            minEvidence=minEvidence,
        ),
    )


def _evidence(
    *,
    ref: str = "Supplier",
    channel: str = KNOWLEDGE_CHANNEL_GRAPH,
    releaseId: int | None = 7,
    releaseVersion: str | None = "2026.11.16-1",
    source: str = KNOWLEDGE_SOURCE_RELEASE,
) -> KnowledgeEvidenceRead:
    return KnowledgeEvidenceRead(
        channel=channel,
        kind="CLASS",
        ref=ref,
        title=ref,
        snippet="片段",
        release_id=releaseId,
        release_version=releaseVersion,
        source=source,
    )


def _response(
    *,
    statuses: dict[str, str] | None = None,
    evidence: tuple[KnowledgeEvidenceRead, ...] | None = None,
    release: KnowledgeReleaseRef | None = None,
) -> KnowledgeQueryResponse:
    rows = (
        {
            KNOWLEDGE_CHANNEL_GRAPH: KNOWLEDGE_ROUTE_OK,
            KNOWLEDGE_CHANNEL_METRIC: KNOWLEDGE_ROUTE_OK,
            KNOWLEDGE_CHANNEL_RAG: KNOWLEDGE_ROUTE_DEGRADED,
            KNOWLEDGE_CHANNEL_SQL: KNOWLEDGE_ROUTE_OK,
        }
        if statuses is None
        else statuses
    )
    return KnowledgeQueryResponse(
        question="供应商交付天数",
        domain="PROCUREMENT",
        release=release
        or KnowledgeReleaseRef(
            id=7, version="2026.11.16-1", manifest_hash="a" * 64, degraded=False
        ),
        answer_draft="口径版本 2026.11.16-1",
        evidence=evidence if evidence is not None else (_evidence(),),
        route_results=tuple(
            KnowledgeRouteResultRead(
                channel=channel,
                status=status,
                item_count=1 if status == KNOWLEDGE_ROUTE_OK else 0,
            )
            for channel, status in rows.items()
        ),
    )


def _failed(result: Any) -> list[str]:
    return [check.name for check in result.checks if not check.passed]


# --- 判定逻辑 ----------------------------------------------------------------


def test_matchingResponsePassesAllChecks() -> None:
    """一切符合期望 ⇒ 通过（无失败判据）。"""
    result = evaluateResponse(_response(), _question())

    assert result.passed is True
    assert _failed(result) == []


def test_visibleExpectationFailsWhenNoRouteRan() -> None:
    """期望可见但**一路都没结果** ⇒ 不通过（编排层没跑 ≠ 有权查）。

    ⚠️ 空 `routeResults` 曾被判成「至少一路有权查」⇒ 假绿。
    """
    response = KnowledgeQueryResponse(
        question="供应商交付天数",
        domain="PROCUREMENT",
        release=KnowledgeReleaseRef(
            id=7, version="2026.11.16-1", manifest_hash="a" * 64, degraded=False
        ),
        answer_draft="",
        evidence=(),
        route_results=(),
    )

    result = evaluateResponse(response, _question())

    assert result.passed is False
    assert "visibility" in _failed(result)


def test_visibleExpectationFailsWhenEveryRouteIsDenied() -> None:
    response = _response(
        statuses={
            KNOWLEDGE_CHANNEL_GRAPH: KNOWLEDGE_ROUTE_DENIED,
            KNOWLEDGE_CHANNEL_METRIC: KNOWLEDGE_ROUTE_DENIED,
            KNOWLEDGE_CHANNEL_RAG: KNOWLEDGE_ROUTE_DENIED,
            KNOWLEDGE_CHANNEL_SQL: KNOWLEDGE_ROUTE_DENIED,
        },
        evidence=(),
    )

    result = evaluateResponse(response, _question())

    assert result.passed is False
    assert "visibility" in _failed(result)


def test_deniedExpectationPassesOnAllDeniedAndNoEvidence() -> None:
    """期望不可见 ⇒ 四路 `DENIED` + 零证据 ⇒ 通过。"""
    response = _response(
        statuses={
            KNOWLEDGE_CHANNEL_GRAPH: KNOWLEDGE_ROUTE_DENIED,
            KNOWLEDGE_CHANNEL_METRIC: KNOWLEDGE_ROUTE_DENIED,
            KNOWLEDGE_CHANNEL_RAG: KNOWLEDGE_ROUTE_DENIED,
            KNOWLEDGE_CHANNEL_SQL: KNOWLEDGE_ROUTE_DENIED,
        },
        evidence=(),
        release=KnowledgeReleaseRef(degraded=True),
    )
    question = _question(visibility="DENIED", channels={}, evidenceRefs=(), minEvidence=0)

    result = evaluateResponse(response, question)

    assert result.passed is True
    assert _failed(result) == []


def test_deniedExpectationFailsWhenEvidenceLeaks() -> None:
    """期望不可见却有证据 ⇒ 不通过（越权泄漏）。"""
    response = _response(
        statuses={
            KNOWLEDGE_CHANNEL_GRAPH: KNOWLEDGE_ROUTE_DENIED,
            KNOWLEDGE_CHANNEL_METRIC: KNOWLEDGE_ROUTE_DENIED,
            KNOWLEDGE_CHANNEL_RAG: KNOWLEDGE_ROUTE_DENIED,
            KNOWLEDGE_CHANNEL_SQL: KNOWLEDGE_ROUTE_DENIED,
        },
        evidence=(_evidence(),),
        release=KnowledgeReleaseRef(degraded=True),
    )
    question = _question(visibility="DENIED", channels={}, evidenceRefs=(), minEvidence=0)

    result = evaluateResponse(response, question)

    assert result.passed is False
    assert "visibility" in _failed(result)


def test_channelStatusMismatchFails() -> None:
    """期望 `SQL=OK` 却是 `EMPTY` ⇒ 不通过。"""
    response = _response(
        statuses={
            KNOWLEDGE_CHANNEL_GRAPH: KNOWLEDGE_ROUTE_OK,
            KNOWLEDGE_CHANNEL_METRIC: KNOWLEDGE_ROUTE_OK,
            KNOWLEDGE_CHANNEL_RAG: KNOWLEDGE_ROUTE_DEGRADED,
            KNOWLEDGE_CHANNEL_SQL: KNOWLEDGE_ROUTE_EMPTY,
        }
    )

    result = evaluateResponse(response, _question())

    assert result.passed is False
    assert "channels" in _failed(result)


def test_missingExpectedEvidenceRefFails() -> None:
    """期望命中 `supplier_delivery_days` 却没有 ⇒ 不通过。"""
    question = _question(evidenceRefs=("Supplier", "supplier_delivery_days"))

    result = evaluateResponse(_response(), question)

    assert result.passed is False
    assert "evidence_refs" in _failed(result)


def test_evidenceWithoutReleaseVersionFailsCitationCheck() -> None:
    """证据缺版本号 ⇒ 引用不可回溯 ⇒ 不通过。"""
    response = _response(evidence=(_evidence(releaseId=None, releaseVersion=None),))

    result = evaluateResponse(response, _question())

    assert result.passed is False
    assert "citation_traceable" in _failed(result)


def test_liveSourcedEvidenceFailsCitationCheck() -> None:
    """`source=LIVE`（直读治理表）⇒ 不是发布读 ⇒ 不通过。"""
    response = _response(evidence=(_evidence(source=KNOWLEDGE_SOURCE_LIVE),))

    result = evaluateResponse(response, _question())

    assert result.passed is False
    assert "citation_traceable" in _failed(result)


def test_minEvidenceFailsWhenTooFew() -> None:
    """证据条数不足 ⇒ 不通过。"""
    question = _question(minEvidence=3)

    result = evaluateResponse(_response(), question)

    assert result.passed is False
    assert "min_evidence" in _failed(result)


def test_degradedReleaseWithEvidenceFailsCitationCheck() -> None:
    """响应说「降级」却有证据 ⇒ 自相矛盾 ⇒ 不通过。"""
    response = _response(release=KnowledgeReleaseRef(degraded=True))

    result = evaluateResponse(response, _question())

    assert result.passed is False
    assert "citation_traceable" in _failed(result)


# --- 问题集解析 --------------------------------------------------------------


def test_parseQuestionSetReadsQuestions() -> None:
    """问题集 JSON ⇒ 题目列表（身份 / 期望都带出来）。"""
    payload = {
        "version": "pilot-1",
        "questions": [
            {
                "id": "P-01",
                "question": "供应商交付天数",
                "domain": "PROCUREMENT",
                "identity": {"roles": ["user"], "departments": ["PROCUREMENT_ORG"]},
                "expect": {
                    "visibility": "VISIBLE",
                    "channels": {"GRAPH": "OK"},
                    "evidenceRefs": ["Supplier"],
                    "minEvidence": 1,
                },
            }
        ],
    }

    questions = parseQuestionSet(payload)

    assert len(questions) == 1
    assert questions[0].id == "P-01"
    assert questions[0].identity.departments == ("PROCUREMENT_ORG",)
    assert questions[0].expect.channels == {"GRAPH": "OK"}
    assert questions[0].expect.evidenceRefs == ("Supplier",)


def test_parseQuestionSetRejectsUnknownVisibility() -> None:
    """未知可见性取值 ⇒ 解析失败（不许默默当 VISIBLE）。"""
    payload = {
        "questions": [
            {
                "id": "P-01",
                "question": "x",
                "domain": "PROCUREMENT",
                "identity": {"roles": [], "departments": []},
                "expect": {"visibility": "MAYBE"},
            }
        ],
    }

    try:
        parseQuestionSet(payload)
    except ValueError as exc:
        assert "visibility" in str(exc)
    else:  # pragma: no cover —— 解析失败才是期望结果
        raise AssertionError("未知 visibility 应当解析失败")


def test_parseQuestionSetReadsRequestScope() -> None:
    """问题集里的请求侧收窄口径 ⇒ 解析成 `KnowledgeScopeRequest`。"""
    payload = {
        "questions": [
            {
                "id": "P-13",
                "question": "x",
                "domain": "PROCUREMENT",
                "identity": {"roles": [], "departments": []},
                "expect": {"visibility": "DENIED"},
                "scope": {"orgCodes": ["QUALITY_ORG"]},
            }
        ],
    }

    questions = parseQuestionSet(payload)

    assert questions[0].scope is not None
    assert questions[0].scope.org_codes == ("QUALITY_ORG",)


def test_parseQuestionSetWithoutScopeKeepsNone() -> None:
    """没写 scope ⇒ 不收窄（`None`），不是空对象。"""
    payload = {
        "questions": [
            {
                "id": "P-01",
                "question": "x",
                "domain": "PROCUREMENT",
                "identity": {"roles": [], "departments": []},
                "expect": {},
            }
        ],
    }

    questions = parseQuestionSet(payload)

    assert questions[0].scope is None


def test_parseQuestionSetRejectsIllegalScope() -> None:
    """非法收窄口径（安全等级取值不在枚举内）⇒ 解析失败。"""
    payload = {
        "questions": [
            {
                "id": "P-01",
                "question": "x",
                "domain": "PROCUREMENT",
                "identity": {"roles": [], "departments": []},
                "expect": {},
                "scope": {"maxSecurityLevel": "L9"},
            }
        ],
    }

    try:
        parseQuestionSet(payload)
    except ValueError as exc:
        assert "scope" in str(exc)
    else:  # pragma: no cover —— 解析失败才是期望结果
        raise AssertionError("非法 scope 应当解析失败")
