"""研究状态机的**阶段恢复态**构件（Task 8 从 `research_agent_ports.py` 抽出）。

抽出动因：Task 8 需要在 ports 的 §4.5 事件词汇里补 `research.step.sql / step.data /
step.chart` 三个事件名，而 ports 已 798 行、距 800 行硬上限仅 2 行（ledger 的余量警告：
「下次增量先抽 research_agent_stages.py」）。本模块承载与**阶段推进**直接相关的一组
无状态构件：checkpoint 决策后的恢复轮内容、恢复态重建与改写态。

依赖方向**单向**：本模块 → ports（只读 `OPT_*` 词汇常量），ports 不反向导入本模块。
调用方（`research_agent_service.py`）按名导入，行为与抽出前逐字一致。
"""

from __future__ import annotations

import uuid
from typing import Any

from app.services.research_agent_ports import (
    OPT_ARMS,
    OPT_CANDIDATES,
    OPT_NEXT_STEP,
    OPT_PLAN,
    OPT_STEP_RESULTS,
)


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


def rebuildState(row: Any, checkpoint: Any) -> dict[str, Any]:
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
