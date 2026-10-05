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
    OPT_STEPS_EXECUTED,
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
        # 已执行步数（Task 14 / N1）：degraded 口径跨恢复轮的**唯一**依据，缺键按 0（fail-safe）
        "stepsExecuted": int(options.get(OPT_STEPS_EXECUTED) or 0),
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
        "stepsExecuted": 0,
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


# ---------------------------------------------------------------------------
# 检查点问句构造（feat-research-entry-ux-fixes W1-a）
# ---------------------------------------------------------------------------
#
# 动因：这两个问句原先是 `research_agent_service.py` 里**无插值的死字面量**
# （用户反馈第 2/3 条：「提示检测到语义歧义，但不知道是什么歧义」「提示验证哪些
# 假设，但没有任何提示、毫无头绪」）。抽到本模块后，文案带上对象（条数 / 种类），
# 且可被纯单测钉住 —— 不必驱动整条研究状态机去断言一句文案。
#
# 只改文案：**绝不改** checkpoint 的 phase / options 结构，已落库的
# `research_checkpoint.options` 兼容性依赖结构稳定。

# 冲突种类 → 中文标签。未知 kind 回落通用词、不抛错：ESL 侧 `_detectConflicts`
# 未来新增 kind 时，本表与本函数都不该因此崩。
_CONFLICT_KIND_LABELS: dict[str, str] = {
    "metric_ambiguous": "指标歧义",
    "wiki_disagree": "知识冲突",
}
_CONFLICT_KIND_FALLBACK = "待确认项"

# 冲突种类无法解析时的兜底问句（缺 kind 字段 / kind 为空串）。
_AMBIGUITY_PROMPT_FALLBACK = "检测到语义歧义，请确认采用哪一项？"

# 候选假设为空时的空态文案（降级路径：无 LLM / 解析失败）。
_EMPTY_CANDIDATES_PROMPT = (
    "本轮未生成候选假设（模型不可用或解析失败），可点「修改」补充研究方向。"
)


def conflictKindLabel(kind: str) -> str:
    """冲突种类的人类可读标签；未知种类回落通用词。"""
    return _CONFLICT_KIND_LABELS.get(kind, _CONFLICT_KIND_FALLBACK)


def ambiguityPrompt(conflicts: list[dict[str, Any]]) -> str:
    """`runtime_dynamic` 相位的问句：带冲突条数 + 去重后的种类清单。"""
    kinds = sorted({str(item.get("kind") or "") for item in conflicts if isinstance(item, dict)})
    labels = "、".join(conflictKindLabel(kind) for kind in kinds if kind != "")
    if labels == "":
        return _AMBIGUITY_PROMPT_FALLBACK
    return f"检测到 {len(conflicts)} 处语义歧义（{labels}），请确认采用哪一项？"


def hypothesisPrompt(candidates: list[dict[str, Any]]) -> str:
    """`hypothesis` 相位的问句：带候选条数；空候选时给下一步指引。"""
    if not candidates:
        return _EMPTY_CANDIDATES_PROMPT
    return f"共 {len(candidates)} 条候选假设，请选择要验证的（可多选）："
