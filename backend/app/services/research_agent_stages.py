"""研究状态机的**阶段恢复态 / 阶段提示词**构件（Task 8 从 `research_agent_ports.py` 抽出）。

抽出动因：ports 承载共享词汇 + 端口 + 无状态构件，一度接近 800 行硬上限；本模块承接
与**阶段推进**直接相关的一组无状态构件：checkpoint 决策后的恢复轮内容、恢复态重建与
改写态，以及假设筛选与打分 / 提示词取值助手（Task 8.5 为给 error code SSOT 腾行数再抽）。

依赖方向**单向**：本模块 → ports（只读 `OPT_*` / 常量词汇），ports 不反向导入本模块。
调用方（`research_agent_service.py` / `research_agent_execution.py`）按名导入，行为与抽出前逐字一致。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.domain.research_models import ResearchSession
from app.services.research_agent_ports import (
    DEFAULT_CANDIDATE_CONFIDENCE,
    DRIVER_HINT_LIMIT,
    MAX_VERIFY_HYPOTHESES,
    OPT_ARMS,
    OPT_CANDIDATES,
    OPT_NEXT_STEP,
    OPT_PLAN,
    OPT_STEP_RESULTS,
)

logger = logging.getLogger(__name__)


def resumeTurnContent(
    *, action: str, choice: dict[str, Any], checkpointId: uuid.UUID, rewritten: str
) -> dict[str, Any]:
    """恢复轮的 user turn 内容；带改写问题时一并记录新问题（可追溯）。"""
    content: dict[str, Any] = {
        "action": action,
        "choice": choice or {},
        "checkpointId": str(checkpointId),
    }
    if rewritten:
        content["question"] = rewritten
    return content


def rebuildState(row: ResearchSession, checkpoint: Any) -> dict[str, Any]:
    """恢复态：从会话种子 + checkpoint.options 的语义载荷重建（不重跑 LLM 段）。"""
    options = checkpoint.options or {}
    return {
        "question": row.input_seed or "",
        "mode": row.mode,
        "userId": row.created_by,
        "esl": options.get(OPT_ARMS),
        "plan": options.get(OPT_PLAN),
        "stepResults": list(options.get(OPT_STEP_RESULTS) or []),
        "hypotheses": list(options.get(OPT_CANDIDATES) or []),
        "resumeStepIndex": int(options.get(OPT_NEXT_STEP) or 0),
        "choice": {},
    }


def rewriteState(state: dict[str, Any], question: str) -> dict[str, Any]:
    """改写问题后的恢复态：换问题 + 清空派生数据（强制重跑 ESL，旧三臂不得带进新问题）。"""
    return {
        **state,
        "question": question,
        "esl": None,
        "plan": None,
        "stepResults": [],
        "hypotheses": [],
        "resumeStepIndex": 0,
    }


# ---------------------------------------------------------------------------
# 假设筛选与打分 / 提示词取值助手（Task 8.5 从 ports 抽出）
# ---------------------------------------------------------------------------


def selectedHypotheses(state: dict[str, Any]) -> list[dict[str, Any]]:
    """按用户在固定 #3 的选择（choice["selectedIndexes"]）筛假设；缺省全选。"""
    candidates = list(state.get("hypotheses") or [])
    indexes = (state.get("choice") or {}).get("selectedIndexes")
    if not isinstance(indexes, list):
        return candidates[:MAX_VERIFY_HYPOTHESES]
    picked: list[dict[str, Any]] = []
    for raw in indexes:
        if isinstance(raw, int) and 0 <= raw < len(candidates):
            picked.append(candidates[raw])
        else:
            logger.warning("假设选择下标越界，忽略: %s", raw)
    return picked[:MAX_VERIFY_HYPOTHESES]


def candidateConfidence(state: dict[str, Any], candidate: dict[str, Any]) -> float:
    """候选分：假设 driver 命中 ESL 指标/本体时的置信度；未命中取基线。"""
    driver = str(candidate.get("driver") or "").strip().lower()
    if driver:
        for name, score in confidenceIndex(state):
            if driver in name.lower():
                return score
    return DEFAULT_CANDIDATE_CONFIDENCE


def confidenceIndex(state: dict[str, Any]) -> list[tuple[str, float]]:
    esl = state.get("esl") or {}
    pairs: list[tuple[Any, Any]] = []
    for metric in esl.get("metrics") or []:
        pairs += [(metric.get("displayName"), metric.get("confidence"))]
        pairs += [(metric.get("kpiCode"), metric.get("confidence"))]
    for obj in esl.get("businessObjects") or []:
        pairs += [(obj.get("className"), obj.get("confidence"))]
        pairs += [(obj.get("matchedAlias"), obj.get("confidence"))]
    return [(str(name), float(score)) for name, score in pairs if name and score is not None]


def drivers(state: dict[str, Any]) -> list[str]:
    """driver 提示：ESL 命中的指标 / 本体名（真实列名提示需 OntologyService，见报告）。"""
    esl = state.get("esl") or {}
    names: list[str] = []
    for metric in esl.get("metrics") or []:
        names += [metric.get("kpiCode"), metric.get("displayName")]
    for obj in esl.get("businessObjects") or []:
        names.append(obj.get("className"))
    seen: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.append(str(name))
    return seen[:DRIVER_HINT_LIMIT]


def dataSummary(state: dict[str, Any]) -> str:
    """喂给假设生成的执行摘要（只用步摘要，不塞原始行）。"""
    lines = [
        f"步骤 {int(r.get('index', 0)) + 1} {r.get('description') or ''}：{r.get('summary') or ''}"
        for r in state.get("stepResults") or []
    ]
    return "\n".join(lines) or "（本轮未取到数据）"


def eslClasses(state: dict[str, Any]) -> list[str]:
    """喂给计划器的本体类提示：ESL 命中 BO 的物理表名。"""
    esl = state.get("esl") or {}
    return [str(o["sourceTable"]) for o in esl.get("businessObjects") or [] if o.get("sourceTable")]


def clientModelName(client: Any) -> str | None:
    """客户端固化的模型名快照（OpenAiClient._modelName，openai_client.py:75）。"""
    return getattr(client, "_modelName", None) or getattr(client, "model_name", None)
