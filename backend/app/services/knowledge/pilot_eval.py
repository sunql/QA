"""P4 试点跑批的**判定逻辑**（纯函数）。

为什么要单独一个模块
--------------------
试点问题集的通过率判据（计划 §6）是**可客观判定**的三项：权限结果 + 证据命中 +
引用可回溯。这三项若藏在跑批脚本里，就**没法单测**（脚本一改，判据跟着变，
且没人说得清「通过」到底是怎么算的）。

⇒ 本模块只做**纯函数**：输入 = 一条响应 + 一条期望，输出 = 逐项判据的通过情况。
脚本（`backend/scripts/knowledge_pilot_runner.py`）只负责取数 / 调 facade / 打印。

三条判据（全部**同时**成立才算通过）
--------------------------------
1. **权限结果**：期望可见 ⇒ 至少一路不是 `DENIED`；期望不可见 ⇒ 四路**全** `DENIED`
   且**零证据**（越权泄漏直接判负）。
2. **证据命中**：期望的类名 / 指标名 / KPI 码 / JOIN 键都出现在证据 `ref` 里。
3. **引用可回溯**：每条证据都带 `releaseId` + `releaseVersion` + `source=RELEASE`；
   响应自称「降级」却有证据 ⇒ 自相矛盾 ⇒ 判负。

⚠️ `EMPTY` 与 `DENIED` **必须分开**（裁决 4 / 4 的补充）：`DENIED` = 域不可见，
`EMPTY` = 查了但没命中 ⇒ 试点里「查得到」与「有权查」是两件事，分开判。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.domain.knowledge_schemas import (
    KNOWLEDGE_ROUTE_DENIED,
    KNOWLEDGE_SOURCE_RELEASE,
    KnowledgeQueryResponse,
    KnowledgeScopeRequest,
)

#: 期望的可见性取值。
VISIBILITY_VISIBLE = "VISIBLE"
VISIBILITY_DENIED = "DENIED"
VISIBILITY_VALUES: tuple[str, ...] = (VISIBILITY_VISIBLE, VISIBILITY_DENIED)


@dataclass(frozen=True, slots=True)
class PilotIdentity:
    """试点问题使用的**调用方身份**（角色 + 组织）。

    只用「角色 + 组织」两个维度：与 `permission.resolveKnowledgeScope` 的输入
    完全一致 ⇒ 试点跑的**就是**服务端那套可见性推导，不是另一套模拟。
    """

    roles: tuple[str, ...] = ()
    departments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PilotExpectation:
    """一条问题的期望结果。

    Attributes:
        visibility: `VISIBLE`（有权查）/ `DENIED`（域不可见）。
        channels: 期望的「通道 → 路由状态」（只写关心的通道；不写 = 不判）。
        evidenceRefs: 期望命中的证据 `ref`（类名 / 指标名 / KPI 码 / JOIN 键）。
        minEvidence: 期望的**最少**证据条数。
    """

    visibility: str = VISIBILITY_VISIBLE
    channels: Mapping[str, str] = field(default_factory=dict)
    evidenceRefs: tuple[str, ...] = ()
    minEvidence: int = 0


@dataclass(frozen=True, slots=True)
class PilotQuestion:
    """一条试点问题（问题 + 身份 + 期望）。

    Attributes:
        scope: 请求侧收窄口径（**只能收窄**，不能放宽）；`None` ⇒ 不收窄。
    """

    id: str
    question: str
    domain: str
    identity: PilotIdentity
    expect: PilotExpectation
    scope: KnowledgeScopeRequest | None = None


@dataclass(frozen=True, slots=True)
class PilotCheck:
    """一条判据的结果（名字 + 通过与否 + 说明）。"""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PilotResult:
    """一条问题的判定结果。"""

    id: str
    question: str
    passed: bool
    checks: tuple[PilotCheck, ...] = ()

    def failures(self) -> tuple[str, ...]:
        """未通过的判据名（报告里用）。"""
        return tuple(check.name for check in self.checks if not check.passed)


def _statusByChannel(response: KnowledgeQueryResponse) -> dict[str, str]:
    return {row.channel: row.status for row in response.route_results}


def _checkVisibility(
    response: KnowledgeQueryResponse, question: PilotQuestion
) -> PilotCheck:
    statuses = _statusByChannel(response)
    allDenied = bool(statuses) and all(
        status == KNOWLEDGE_ROUTE_DENIED for status in statuses.values()
    )
    if question.expect.visibility == VISIBILITY_DENIED:
        passed = allDenied and not response.evidence
        detail = (
            "四路均不可见且零证据"
            if passed
            else f"期望不可见，实际状态={statuses or '无'}，证据数={len(response.evidence)}"
        )
        return PilotCheck(name="visibility", passed=passed, detail=detail)
    # ⚠️ 「一路都没结果」**不算**有权查：那是编排层没跑（或响应被截断）⇒ 必须红。
    passed = bool(statuses) and not allDenied
    if passed:
        detail = "至少一路有权查"
    elif not statuses:
        detail = "期望可见，实际没有任何路由结果"
    else:
        detail = f"期望可见，实际四路全为 {KNOWLEDGE_ROUTE_DENIED}"
    return PilotCheck(name="visibility", passed=passed, detail=detail)


def _checkChannels(
    response: KnowledgeQueryResponse, question: PilotQuestion
) -> PilotCheck:
    statuses = _statusByChannel(response)
    wrong: list[str] = []
    for channel, expected in question.expect.channels.items():
        actual = statuses.get(channel)
        if actual != expected:
            wrong.append(f"{channel}: 期望 {expected}，实际 {actual or '缺失'}")
    return PilotCheck(
        name="channels",
        passed=not wrong,
        detail="；".join(wrong) if wrong else "各通道状态与期望一致",
    )


def _checkEvidenceRefs(
    response: KnowledgeQueryResponse, question: PilotQuestion
) -> PilotCheck:
    actual = {item.ref.strip().lower() for item in response.evidence}
    missing = [
        ref for ref in question.expect.evidenceRefs if ref.strip().lower() not in actual
    ]
    return PilotCheck(
        name="evidence_refs",
        passed=not missing,
        detail=(
            f"缺少期望证据：{'、'.join(missing)}"
            if missing
            else f"命中期望证据 {len(question.expect.evidenceRefs)} 项"
        ),
    )


def _checkCitationTraceable(
    response: KnowledgeQueryResponse, question: PilotQuestion
) -> PilotCheck:
    if response.release.degraded and response.evidence:
        return PilotCheck(
            name="citation_traceable",
            passed=False,
            detail="响应标记为降级，却带了证据（自相矛盾）",
        )
    problems: list[str] = []
    for item in response.evidence:
        if item.source != KNOWLEDGE_SOURCE_RELEASE:
            problems.append(f"{item.ref}: source={item.source}")
            continue
        if item.release_id is None or item.release_id != response.release.id:
            problems.append(f"{item.ref}: releaseId={item.release_id}")
            continue
        if not item.release_version or item.release_version != response.release.version:
            problems.append(f"{item.ref}: releaseVersion={item.release_version}")
    return PilotCheck(
        name="citation_traceable",
        passed=not problems,
        detail=("；".join(problems) if problems else f"全部证据可回溯到 {response.release.version}"),
    )


def _checkMinEvidence(
    response: KnowledgeQueryResponse, question: PilotQuestion
) -> PilotCheck:
    count = len(response.evidence)
    return PilotCheck(
        name="min_evidence",
        passed=count >= question.expect.minEvidence,
        detail=f"证据 {count} 条，期望 ≥ {question.expect.minEvidence} 条",
    )


def evaluateResponse(
    response: KnowledgeQueryResponse, question: PilotQuestion
) -> PilotResult:
    """判定一条响应是否满足期望。

    Args:
        response: facade 的响应。
        question: 试点问题（含身份与期望）。

    Returns:
        `PilotResult`：逐项判据 + 总体通过与否。
    """
    checks = (
        _checkVisibility(response, question),
        _checkChannels(response, question),
        _checkEvidenceRefs(response, question),
        _checkCitationTraceable(response, question),
        _checkMinEvidence(response, question),
    )
    return PilotResult(
        id=question.id,
        question=question.question,
        passed=all(check.passed for check in checks),
        checks=checks,
    )


# ---------------------------------------------------------------------------
# 问题集解析
# ---------------------------------------------------------------------------


def _identityOf(payload: Mapping[str, Any]) -> PilotIdentity:
    raw = payload.get("identity") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("identity 必须是对象")
    return PilotIdentity(
        roles=tuple(str(item) for item in raw.get("roles") or ()),
        departments=tuple(str(item) for item in raw.get("departments") or ()),
    )


def _expectationOf(payload: Mapping[str, Any]) -> PilotExpectation:
    if not isinstance(payload, Mapping):
        raise ValueError("expect 必须是对象")
    visibility = str(payload.get("visibility") or VISIBILITY_VISIBLE)
    if visibility not in VISIBILITY_VALUES:
        raise ValueError(
            f"visibility 只能是 {'/'.join(VISIBILITY_VALUES)}，实际：{visibility}"
        )
    channels = payload.get("channels") or {}
    if not isinstance(channels, Mapping):
        raise ValueError("expect.channels 必须是对象")
    refs = payload.get("evidenceRefs") or ()
    if not isinstance(refs, (list, tuple)):
        raise ValueError("expect.evidenceRefs 必须是数组")
    return PilotExpectation(
        visibility=visibility,
        channels={str(k): str(v) for k, v in channels.items()},
        evidenceRefs=tuple(str(item) for item in refs),
        minEvidence=int(payload.get("minEvidence") or 0),
    )


def _scopeOf(payload: Mapping[str, Any]) -> KnowledgeScopeRequest | None:
    """请求侧收窄口径（缺省 = 不收窄）。

    非法取值（Pydantic 拒的枚举 / 超长列表）⇒ 抛 `ValueError`
    ⇒ **解析期失败比跑批期失败好查**。
    """
    raw = payload.get("scope")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("scope 必须是对象")
    try:
        return KnowledgeScopeRequest.model_validate(dict(raw))
    except Exception as exc:  # noqa: BLE001 —— Pydantic 的校验错误统一转成 ValueError
        raise ValueError(f"scope 非法：{exc}") from exc


def parseQuestionSet(payload: Mapping[str, Any]) -> tuple[PilotQuestion, ...]:
    """把问题集 JSON 解析成题目列表。

    Args:
        payload: 问题集正文（`{"questions": [...]}`）。

    Returns:
        题目列表（顺序与文件一致）。

    Raises:
        ValueError: 结构不对或取值非法（**解析期失败比跑批期失败好查**）。
    """
    rawQuestions = payload.get("questions")
    if not isinstance(rawQuestions, (list, tuple)):
        raise ValueError("问题集必须含 questions 数组")
    questions: list[PilotQuestion] = []
    seen: set[str] = set()
    for raw in rawQuestions:
        if not isinstance(raw, Mapping):
            raise ValueError("questions 的每一项必须是对象")
        questionId = str(raw.get("id") or "")
        if not questionId:
            raise ValueError("每条问题必须有 id")
        if questionId in seen:
            raise ValueError(f"问题 id 重复：{questionId}")
        seen.add(questionId)
        questions.append(
            PilotQuestion(
                id=questionId,
                question=str(raw.get("question") or ""),
                domain=str(raw.get("domain") or ""),
                identity=_identityOf(raw),
                expect=_expectationOf(raw.get("expect") or {}),
                scope=_scopeOf(raw),
            )
        )
    return tuple(questions)


__all__ = [
    "VISIBILITY_DENIED",
    "VISIBILITY_VALUES",
    "VISIBILITY_VISIBLE",
    "PilotCheck",
    "PilotExpectation",
    "PilotIdentity",
    "PilotQuestion",
    "PilotResult",
    "evaluateResponse",
    "parseQuestionSet",
]
