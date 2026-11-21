"""A5 评估器的运行时编排：问题集加载、档案读写、实跑/前置检查。

CLI 与报告在 `eval_semantic_answer_parity.py`；比较判定在 `answer_parity_compare.py`。
这里只负责「拿到样本」与「把样本变成退出码」，不实现判定规则。

复用 D17 的两态机制（`_setSwitch` / `_render` / `_generate` / `_guardVerdict` /
`_contentDiff`）是有意为之：M0.4 §4.7 要求新增脚本、不改现有脚本；复制一份会让
两条口径慢慢漂移。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts import answer_parity_compare as parity
from scripts.db_snapshot_guard import buildSessionFactory
from scripts.diag_semantic_switchover import (
    _VIOLATING_SQL,
    _contentDiff,
    _generate,
    _guardVerdict,
    _orderDiffLines,
    _render,
    _setSwitch,
)

RUN_FILE_NAMES = ("off-1", "off-2", "on-1", "on-2")

# 默认类子集：Q01–Q03/Q05 会碰到的采购域表（含价格清单，Q05 需要）。
DEFAULT_SUBSET = (
    "DIM_SUPPLIER",
    "DIM_IMATERIAL",
    "DWD_PURCHASE_ORDER_DTL",
    "DWD_GOODS_RECEIPT_DTL",
    "DWD_ARRIVAL_ORDER_DTL",
    "DWD_SUPPLIER_PRICE_LIST",
    "DWD_SUPPLIER_PRICE_LIST_HEADER",
)


# ---------------------------------------------------------------------------
# 问题集：version + sha256 + 结构校验（外部输入必须 fail fast）
# ---------------------------------------------------------------------------


def _validateQuestions(questions: list[Any]) -> None:
    seen: set[str] = set()
    for index, item in enumerate(questions):
        if not isinstance(item, dict):
            raise ValueError(f"问题集第 {index + 1} 项不是对象")
        questionId = item.get("id")
        if not isinstance(questionId, str) or not questionId.strip():
            raise ValueError(f"问题集第 {index + 1} 项缺 id")
        if questionId in seen:
            raise ValueError(f"问题集 id 重复：{questionId}")
        seen.add(questionId)
        applicable = item.get("parityApplicable", False)
        if not isinstance(applicable, bool):
            # 字符串 "true" 会被 `is True` 静默排除 ⇒ 用于开闸的工具不能容忍
            raise ValueError(f"{questionId} 的 parityApplicable 必须是布尔值")
        if not isinstance(item.get("question", ""), str):
            raise ValueError(f"{questionId} 的 question 必须是字符串")


def loadQuestions(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("问题集顶层必须是对象")
    declared = payload.get("sha256")
    actual = parity.questionsDigest(payload)
    if declared != actual:
        raise ValueError(
            f"问题集摘要不符：文件写 {declared}，实算 {actual}。"
            "改了题目就必须同时改 version 并重算 sha256（M0.4 §4.6）。"
        )
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("问题集为空或不是列表")
    _validateQuestions(questions)
    return payload


def applicableQuestions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in payload["questions"] if item.get("parityApplicable") is True]


# ---------------------------------------------------------------------------
# 档案读写（回放是唯一能判 L2–L5 的路径，输入校验必须严）
# ---------------------------------------------------------------------------


def _validateRecord(path: Path, record: Any) -> None:
    if not isinstance(record, dict):
        raise ValueError(f"{path.name} 有一条记录不是对象")
    if not isinstance(record.get("questionId"), str) or not record["questionId"]:
        raise ValueError(f"{path.name} 有一条记录缺 questionId")
    sql = record.get("sql")
    if sql is not None and not isinstance(sql, str):
        raise ValueError(f"{path.name} / {record['questionId']} 的 sql 不是字符串")
    rows = record.get("rows")
    if rows is not None and (
        not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows)
    ):
        raise ValueError(f"{path.name} / {record['questionId']} 的 rows 不是对象列表")
    answer = record.get("answer")
    if answer is not None and not isinstance(answer, str):
        raise ValueError(f"{path.name} / {record['questionId']} 的 answer 不是字符串")
    releaseId = record.get("semanticReleaseId")
    if releaseId is not None and not isinstance(releaseId, int):
        raise ValueError(f"{path.name} / {record['questionId']} 的 semanticReleaseId 不是整数")


def writeJsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def writeMeta(path: Path, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def readSamples(path: Path) -> dict[str, dict[str, Any]]:
    """读一份档案；**缺文件返回空集**（⇒ 逐题判「无法判定」，退出码 6）。

    档案不全不能当成「跑过了但没问题」：实跑路径四份文件一定都在，
    这里兜的是回放半截目录 —— 那种情况只能判不可归因，不能判通过。
    """
    if not path.exists():
        return {}
    samples: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        _validateRecord(path, record)
        questionId = record["questionId"]
        if questionId in samples:
            raise ValueError(f"{path.name} 里 {questionId} 重复")
        samples[questionId] = record
    return samples


@dataclass(frozen=True)
class QuestionReport:
    questionId: str
    question: str
    evaluation: parity.PairEvaluation


def _releaseGuardError(samples: dict[str, dict[str, dict[str, Any]]]) -> str | None:
    """回放也要证明「ON 真走了快照、OFF 真没走」——否则会把 OFF vs OFF 判成一致。"""
    for name in ("off-1", "off-2"):
        for questionId, record in samples[name].items():
            if record.get("semanticReleaseId") is not None:
                return f"{name} / {questionId} 回报了发布 id ⇒ 开关没关住"
    onIds = {
        record.get("semanticReleaseId")
        for name in ("on-1", "on-2")
        for record in samples[name].values()
    }
    if not onIds:
        return None
    if None in onIds:
        return "on-* 档案有记录没走快照（semanticReleaseId 为空）"
    if len(onIds) != 1:
        return f"on-* 档案跨了多个发布：{sorted(onIds)}"
    return None


def evaluateRunDirectory(
    runDir: Path,
    questions: list[dict[str, Any]],
    requiredLayers: tuple[str, ...],
) -> tuple[int, list[QuestionReport]]:
    """从 4 个 jsonl 重放判定（实跑与档案回放共用**同一套**判定）。"""
    samples = {name: readSamples(runDir / f"{name}.jsonl") for name in RUN_FILE_NAMES}
    releaseError = _releaseGuardError(samples)
    if releaseError is not None:
        raise ValueError(f"档案不足以构成两态证据：{releaseError}")

    reports: list[QuestionReport] = []
    for item in questions:
        questionId = item["id"]
        records = {name: samples[name].get(questionId) or {} for name in RUN_FILE_NAMES}
        evaluation = parity.evaluateQuestion(
            off1=records["off-1"].get("sql"),
            off2=records["off-2"].get("sql"),
            on1=records["on-1"].get("sql"),
            on2=records["on-2"].get("sql"),
            requiredLayers=requiredLayers,
            offRows1=records["off-1"].get("rows"),
            onRows1=records["on-1"].get("rows"),
            offRows2=records["off-2"].get("rows"),
            onRows2=records["on-2"].get("rows"),
            offAnswer1=records["off-1"].get("answer"),
            onAnswer1=records["on-1"].get("answer"),
            offAnswer2=records["off-2"].get("answer"),
            onAnswer2=records["on-2"].get("answer"),
        )
        reports.append(
            QuestionReport(
                questionId=questionId,
                question=item.get("question", ""),
                evaluation=evaluation,
            )
        )
    return (
        parity.aggregateExitCodes([report.evaluation.exitCode for report in reports]),
        reports,
    )


# ---------------------------------------------------------------------------
# 实跑：两态 × 两遍
# ---------------------------------------------------------------------------


async def loadSubset(session, ontology, classNames: tuple[str, ...]) -> tuple[list, list]:
    allClasses = await ontology.listClasses(session)
    byName = {cls.class_name: cls for cls in allClasses}
    missing = [name for name in classNames if name not in byName]
    if missing:
        raise RuntimeError(f"类子集缺类：{missing}")
    classes = [byName[name] for name in classNames]
    joins = await ontology.listJoins(session)
    return classes, joins


def _sampleRecord(
    questionId: str, sql: str, releaseId: int | None, elapsedMs: float
) -> dict[str, Any]:
    return {
        "questionId": questionId,
        "sql": sql,
        "semanticReleaseId": releaseId,
        "elapsedMs": round(elapsedMs, 1),
    }


def _buildLlm(llmKey: str, llmEndpoint: str, llmModel: str):
    from app.domain.enums import ProviderType
    from app.infrastructure.llm.openai_client import OpenAiClient

    settings = SimpleNamespace(
        model_name=llmModel,
        api_endpoint=llmEndpoint,
        temperature=0.0,
        disable_thinking=False,
    )
    modelConfig = SimpleNamespace(
        model_name=llmModel, temperature=0.0, disable_thinking=False
    )
    return OpenAiClient(settings, apiKey=llmKey, provider=ProviderType.OPENAI), modelConfig


def _readOnlyCandidate(sql: str) -> bool:
    return sql.lstrip().upper().startswith(("SELECT", "WITH"))


def _guardProblem(sql: str) -> str | None:
    """只对「本来就是只读语句」的样本下 Guard 结论。

    模型吐出散文/空串是「某态产不出 SQL」（6），不是「合法 SQL 被拒」（5）——
    把生成噪声报成守卫失效会把人带去查错方向。
    """
    if not _readOnlyCandidate(sql):
        return None
    passed, reason = _guardVerdict(sql)
    return None if passed else reason


async def _renderBothStates(session, classes, joins) -> dict[str, Any]:
    _setSwitch(False)
    offText, offReleaseId = await _render(session, classes, joins)
    _setSwitch(True)
    onText, onReleaseId = await _render(session, classes, joins)
    return {
        "offText": offText,
        "onText": onText,
        "offReleaseId": offReleaseId,
        "onReleaseId": onReleaseId,
    }


async def _sampleState(
    *,
    stateEnabled: bool,
    stateName: str,
    runIndex: int,
    llmClient,
    modelConfig,
    questions: list[dict[str, Any]],
    classes,
    joins,
    session,
    activeId: int,
    runDir: Path,
    guardBroken: list[str],
) -> None:
    expectedId = activeId if stateEnabled else None
    records: list[dict[str, Any]] = []
    for item in questions:
        startedAt = time.perf_counter()
        sql, releaseId = await _generate(
            llmClient, modelConfig, item["question"], classes, joins, session
        )
        elapsedMs = (time.perf_counter() - startedAt) * 1000.0
        if releaseId != expectedId:
            raise RuntimeError(
                f"{stateName}-{runIndex} / {item['id']}：发布 id 回报 {releaseId}，"
                f"期望 {expectedId} ⇒ 该轮不构成有效证据"
            )
        guardProblem = _guardProblem(sql)
        if guardProblem:
            guardBroken.append(f"{stateName}-{runIndex} / {item['id']}：{guardProblem}")
        records.append(_sampleRecord(item["id"], sql, releaseId, elapsedMs))
        print(
            f"  {stateName}-{runIndex} {item['id']} "
            f"({elapsedMs / 1000:.1f}s, releaseId={releaseId}) {sql[:160]}"
        )
    writeJsonl(runDir / f"{stateName}-{runIndex}.jsonl", records)


def _verifyGuardChain(guardBroken: list[str]) -> None:
    for enabled in (False, True):
        _setSwitch(enabled)
        passed, _reason = _guardVerdict(_VIOLATING_SQL)
        if passed:
            guardBroken.append(f"违规 SQL 在开关={'开' if enabled else '关'}时被放行")


@dataclass(frozen=True)
class _PreparedStates:
    """两态渲染的公共前置结果（实跑与前置检查共用）。"""

    activeId: int
    classes: list
    joins: list
    rendered: dict[str, Any]
    gaps: list[str]


async def _prepareStates(
    session,
    classNames: tuple[str, ...],
    meta: dict[str, Any],
    metaPath: Path,
    *,
    includeOrderDiff: bool,
) -> tuple[_PreparedStates | None, tuple[int, dict[str, Any]] | None]:
    """读 ACTIVE 发布 + 渲染两态 + 发布 id 守卫 + schema 内容比对。

    返回 `(prepared, failure)`；`failure` 非 None 时调用方直接返回它
    （已经是可回给 CLI 的 `(exitCode, meta)`）。
    """
    from app.services.ontology_service import OntologyService
    from app.services.semantic_registry_service import semanticRegistry

    active = await semanticRegistry.getActive(session)
    if active is None:
        return None, _finish(
            meta, metaPath, parity.EXIT_PRECONDITION, "precondition-failed",
            "库里没有 ACTIVE 语义发布",
        )

    ontology = OntologyService()
    classes, joins = await loadSubset(session, ontology, classNames)
    print(f"类子集 = {list(classNames)}（{len(classes)} 类），join 数 = {len(joins)}")

    rendered = await _renderBothStates(session, classes, joins)
    meta["release"] = {
        "off": rendered["offReleaseId"],
        "on": rendered["onReleaseId"],
        "active": active.id,
        "activeVersion": active.version,
    }
    if rendered["offReleaseId"] is not None:
        return None, _finish(
            meta, metaPath, parity.EXIT_PRECONDITION, "precondition-failed",
            "开关关时仍回报发布 id ⇒ 开关没关住",
        )
    if rendered["onReleaseId"] != active.id:
        return None, _finish(
            meta, metaPath, parity.EXIT_PRECONDITION, "precondition-failed",
            f"开关开时未走 ACTIVE 快照（回报 {rendered['onReleaseId']}，期望 {active.id}）",
        )

    gaps = _contentDiff(rendered["offText"], rendered["onText"])
    meta["schema"] = {
        "offLines": len(rendered["offText"].splitlines()),
        "onLines": len(rendered["onText"].splitlines()),
        "contentDiffBlocks": len(gaps),
        "contentDiffSample": gaps[:10],
    }
    if includeOrderDiff:
        meta["schema"]["orderDiffLines"] = _orderDiffLines(
            rendered["offText"], rendered["onText"]
        )
    return _PreparedStates(activeId=active.id, classes=classes, joins=joins,
                           rendered=rendered, gaps=gaps), None


async def _sampleAllStates(
    *,
    prepared: _PreparedStates,
    llmClient,
    modelConfig,
    questions: list[dict[str, Any]],
    session,
    runDir: Path,
    guardBroken: list[str],
) -> None:
    runDir.mkdir(parents=True, exist_ok=True)
    for stateEnabled, stateName in ((False, "off"), (True, "on")):
        _setSwitch(stateEnabled)
        for runIndex in (1, 2):
            await _sampleState(
                stateEnabled=stateEnabled,
                stateName=stateName,
                runIndex=runIndex,
                llmClient=llmClient,
                modelConfig=modelConfig,
                questions=questions,
                classes=prepared.classes,
                joins=prepared.joins,
                session=session,
                activeId=prepared.activeId,
                runDir=runDir,
                guardBroken=guardBroken,
            )


async def runLive(
    *,
    databaseUrl: str,
    llmKey: str,
    llmEndpoint: str,
    llmModel: str,
    questions: list[dict[str, Any]],
    classNames: tuple[str, ...],
    runDir: Path,
    requiredLayers: tuple[str, ...],
    meta: dict[str, Any],
    metaPath: Path,
) -> tuple[int, dict[str, Any]]:
    """实跑两态 × 两遍；返回 (退出码, meta)。异常由调用方统一脱敏映射。"""
    llmClient, modelConfig = _buildLlm(llmKey, llmEndpoint, llmModel)
    factory = buildSessionFactory(databaseUrl)
    guardBroken: list[str] = []

    try:
        async with factory() as session:
            prepared, failure = await _prepareStates(
                session, classNames, meta, metaPath, includeOrderDiff=True
            )
            if failure is not None:
                return failure
            writeMeta(metaPath, meta)
            if prepared.gaps:
                return _finish(
                    meta, metaPath, parity.EXIT_NOT_EQUIVALENT, "schema-not-equivalent",
                    f"两态 schema 内容不等价（差异块 {len(prepared.gaps)}）"
                    "⇒ 前置故障，不是评估结论",
                )
            await _sampleAllStates(
                prepared=prepared,
                llmClient=llmClient,
                modelConfig=modelConfig,
                questions=questions,
                session=session,
                runDir=runDir,
                guardBroken=guardBroken,
            )
            _verifyGuardChain(guardBroken)
    finally:
        _setSwitch(False)

    if guardBroken:
        meta["status"] = "guard-broken"
        meta["reason"] = guardBroken[:10]
        writeMeta(metaPath, meta)
        return parity.EXIT_GUARD_BROKEN, meta

    exitCode, reports = evaluateRunDirectory(runDir, questions, requiredLayers)
    meta["status"] = "finished"
    meta["finishedAt"] = datetime.now(UTC).isoformat()
    meta["conclusion"] = _conclusion(exitCode, requiredLayers, reports)
    writeMeta(metaPath, meta)
    return exitCode, meta


def _conclusion(
    exitCode: int,
    requiredLayers: tuple[str, ...],
    reports: list[QuestionReport],
) -> dict[str, Any]:
    return {
        "exitCode": exitCode,
        "requiredLayers": list(requiredLayers),
        "perQuestion": {
            report.questionId: {
                "exitCode": report.evaluation.exitCode,
                "reason": report.evaluation.reason,
                "layers": [
                    {"layer": verdict.layer, "status": verdict.status, "detail": verdict.detail}
                    for verdict in report.evaluation.verdicts
                ],
            }
            for report in reports
        },
    }


def _finish(
    meta: dict[str, Any], metaPath: Path, exitCode: int, status: str, reason: str
) -> tuple[int, dict[str, Any]]:
    meta["status"] = status
    meta["reason"] = reason
    writeMeta(metaPath, meta)
    print(reason, file=sys.stderr)
    return exitCode, meta


# ---------------------------------------------------------------------------
# 前置检查（不调模型）：两态 schema 内容等价 + 发布 id 守卫
# ---------------------------------------------------------------------------


async def runPrecheck(
    *,
    databaseUrl: str,
    classNames: tuple[str, ...],
    meta: dict[str, Any],
    metaPath: Path,
) -> tuple[int, dict[str, Any]]:
    factory = buildSessionFactory(databaseUrl)
    try:
        async with factory() as session:
            prepared, failure = await _prepareStates(
                session, classNames, meta, metaPath, includeOrderDiff=False
            )
            if failure is not None:
                return failure
            if prepared.gaps:
                return _finish(
                    meta, metaPath, parity.EXIT_NOT_EQUIVALENT, "schema-not-equivalent",
                    f"两态 schema 内容不等价（差异块 {len(prepared.gaps)}）",
                )
            meta["status"] = "precheck-passed"
            writeMeta(metaPath, meta)
            print(
                "前置检查通过：schema 内容差异 0；"
                f"OFF 行数={meta['schema']['offLines']} / ON 行数={meta['schema']['onLines']}；"
                f"releaseId={prepared.rendered['onReleaseId']}"
            )
            return parity.EXIT_OK, meta
    finally:
        _setSwitch(False)
