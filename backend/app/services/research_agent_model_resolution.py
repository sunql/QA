"""研究链路的 LLM 模型解析接缝（Task 5 从 research_agent_ports 抽出，保持该文件 ≤ 800 行）。

内容为**逐字搬移**：`buildClient` / `buildRoutingContext` / `PreferredModelUnavailableError`
/ `resolveModelConfig` / `_readConfigs` / `_resolvePreferredConfig` / `markLlmUnavailable`
/ `resolveClient`（原 `research_agent_ports.py:541-734`）。**不改一行逻辑、不改签名**。

依赖方向是单向的：本模块 import `research_agent_ports` 取 `emitEvent` / `errorPayload` /
`ERROR_LLM_UNAVAILABLE` / `LLM_UNAVAILABLE_MESSAGE`（这四个留在 ports.py）；
`research_agent_ports` **不** import 本模块（无环）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.messages_zh import MSG_MODEL_CONFIG_UNAVAILABLE
from app.services.model_router_service import RoutingContext
from app.services.research_agent_ports import (
    ERROR_LLM_UNAVAILABLE,
    EVENT_ERROR,
    LLM_UNAVAILABLE_MESSAGE,
    Emit,
    emitEvent,
    errorPayload,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 无状态构件：LLM 客户端兜底 / 用量记账兜底
# （Task 6 从 research_agent_service 抽出，保持该文件 ≤ 800 行）
# ---------------------------------------------------------------------------


def buildClient(factory: Any, config: Any) -> Any:
    """按注入 factory + **已选模型配置**取客户端；创建失败降级为「无 LLM」并留痕。

    `config is None` 表示「无可用模型配置」——此处**绝不回退 `factory(None)`**：
    那正是 keyless 静默降级的来源（Task 5 MEDIUM-4），也是 Task 6.5-2 要根除的
    `factory(None)` 口径。无配置时直接返回 None，由 `resolveClient` 走显式降级。
    """
    if factory is None or config is None:
        return None
    try:
        return factory(config)
    except Exception:  # noqa: BLE001 —— 客户端创建失败降级为「无 LLM」，不中断 turn
        logger.error("LLM 客户端创建失败，本次降级跳过 LLM 段", exc_info=True)
        return None


async def buildRoutingContext(
    session: AsyncSession | None, sessionId: uuid.UUID | str, tokenUsage: Any
) -> RoutingContext:
    """按 chat 同口径装配路由上下文（对拍 `chat_service._buildRoutingContext`）。

    M1（fix round 1）：此前只传 `sessionId`，`sessionCost` / `sessionTurnCount` /
    `priorModelId` 全取默认值 ⇒ 路由器的**预算超限降级**（`sessionCost >= sessionBudget`
    ⇒ 最便宜）与**会话亲和**（`turnCount < affinityTurns` 且 `priorModelId` 命中 ⇒ 沿用）
    两条规则对研究链路恒不触发，等于路由失效。三项读数一律走 `TokenUsageService` 的公开面
    （getSessionCost / getSessionTurnCount / getLastModelId），不自算、不读 chat 私有。
    """
    if tokenUsage is None or session is None:
        return RoutingContext(sessionId=str(sessionId))
    try:
        # 同 resolveModelConfig：读用量失败也必须 savepoint 隔离，否则主事务留在 aborted 态，后续写入全炸。
        async with session.begin_nested():
            cost = await tokenUsage.getSessionCost(session, str(sessionId))
            turns = await tokenUsage.getSessionTurnCount(session, str(sessionId))
            prior = await tokenUsage.getLastModelId(session, str(sessionId))
    except Exception:  # noqa: BLE001 —— 读用量失败按零上下文路由，不阻断整轮
        logger.warning("装配路由上下文失败，按零上下文路由: session=%s", sessionId, exc_info=True)
        return RoutingContext(sessionId=str(sessionId))
    return RoutingContext(
        sessionId=str(sessionId),
        sessionCost=float(cost or 0),
        sessionTurnCount=int(turns or 0),
        priorModelId=prior,
    )


class PreferredModelUnavailableError(RuntimeError):
    """会话显式选定的模型不可用（不存在 / 已停用 / key 缺失）。

    **绝不静默回落自动路由**（静默替换正是要修的「以为用了 A 实际用了 B」误判）。由
    `_guardedRun` 捕获 → 发 `research.error{turn_failed}`（终态）→ markFailed。
    """


async def resolveModelConfig(
    modelConfigs: Any,
    modelRouter: Any,
    factory: Any,
    session: AsyncSession | None,
    *,
    question: str,
    sessionId: uuid.UUID | str,
    tokenUsage: Any = None,
    preferredModelId: int | None = None,
) -> Any | None:
    """按 chat 同口径选出本轮模型配置：`list(activeOnly)` → 可用性筛 → 路由。

    与 chat `_buildPipelineContext` 同源（Step 0）：
    - 配置清单走公开面 `ModelConfigService.list(session, activeOnly=True)`；
    - 「可用」判据复用注入的 `factory`（真实实现即 `createClient`，key 解析的 SSOT），
      逐配置隔离异常（单条密文损坏不应让整轮路由失败）；
    - 选择走公开面 `ModelRouterService.selectModel(configs, prompt, ctx)`，其中 `ctx`
      由 `buildRoutingContext` 装配（预算 / 轮次 / 上次模型三项齐备）。

    `preferredModelId` 非空表示**会话级显式选定**（W5-b）：按**全量**清单直选、跳过
    router；不可用（不存在 / 已停用 / key 缺失）一律抛 `PreferredModelUnavailableError`，
    不回落。其余任何一步失败/为空都返回 None（调用方走 `research.error` 显式降级），
    不抛错、不静默用 keyless 客户端顶替。
    """
    if modelConfigs is None:
        return None
    if preferredModelId is not None:
        return await _resolvePreferredConfig(
            modelConfigs, factory, session, preferredModelId, sessionId=sessionId
        )
    configs = await _readConfigs(modelConfigs, session, sessionId, activeOnly=True)
    if configs is None:
        logger.warning("读取模型配置失败，本轮 LLM 段降级: session=%s", sessionId)
        return None
    usable = [config for config in configs if buildClient(factory, config) is not None]
    if not usable:
        logger.warning("无可用模型配置（key 缺失/未配置），本轮 LLM 段降级: session=%s", sessionId)
        return None
    if modelRouter is None:
        return usable[0]
    try:
        ctx = await buildRoutingContext(session, sessionId, tokenUsage)
        return modelRouter.selectModel(usable, question, ctx)
    except Exception:  # noqa: BLE001 —— 路由失败（如 NoAvailableModelError）同样降级
        logger.warning("模型路由失败，本轮 LLM 段降级: session=%s", sessionId, exc_info=True)
        return None


async def _readConfigs(
    modelConfigs: Any, session: AsyncSession | None, sessionId: uuid.UUID | str, *,
    activeOnly: bool,
) -> list[Any] | None:
    """读模型配置清单；读失败返回 None（异常在此留痕，调用方只决定降级 / 报错）。

    独立 savepoint（Task 7.5 HIGH）：`list()` 语句级失败（如 llm_config 结构漂移）会把主事务
    置 aborted ⇒ 后续 checkpoint / resume INSERT 全炸（已发 202，数据静默丢失）；savepoint 隔离之。
    """
    try:
        async with session.begin_nested():
            return list(await modelConfigs.list(session, activeOnly=activeOnly))
    except Exception:  # noqa: BLE001 —— 读取失败不在此处置：调用方决定降级或报错
        logger.warning(
            "模型配置读取异常: session=%s activeOnly=%s", sessionId, activeOnly, exc_info=True
        )
        return None


async def _resolvePreferredConfig(
    modelConfigs: Any, factory: Any, session: AsyncSession | None, preferredModelId: int,
    *, sessionId: uuid.UUID | str,
) -> Any:
    """会话显式选定模型的**直选**：读**全量**清单、跳过 router；不可用即显式报错。

    三种不可用（不存在 / 已停用 / key 缺失）统一抛 `PreferredModelUnavailableError`，
    **绝不静默回落自动路由** —— 静默替换正是要修的「以为用了 A 实际用了 B」误判。
    """
    configs = await _readConfigs(modelConfigs, session, sessionId, activeOnly=False)
    if configs is None:
        logger.warning("读取模型配置失败（会话已指定模型）: session=%s", sessionId)
        raise PreferredModelUnavailableError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=preferredModelId))
    target = next((config for config in configs if config.id == preferredModelId), None)
    if target is None or not target.is_active:
        raise PreferredModelUnavailableError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=preferredModelId))
    if buildClient(factory, target) is None:
        # key 缺失 / 客户端建不起来：同样显式报错，不换模型。
        raise PreferredModelUnavailableError(MSG_MODEL_CONFIG_UNAVAILABLE.format(id=preferredModelId))
    return target


async def markLlmUnavailable(
    state: dict[str, Any], *, emit: Emit | None, sessionId: uuid.UUID | str
) -> None:
    """无可用 LLM 的**显式**降级：state 标记 + warning + `research.error`（不静默 done）。"""
    state["llmUnavailable"] = True
    logger.warning("无可用 LLM 客户端，本轮 LLM 段降级: session=%s", sessionId)
    await emitEvent(
        emit,
        EVENT_ERROR,
        errorPayload(ERROR_LLM_UNAVAILABLE, LLM_UNAVAILABLE_MESSAGE),
    )


async def resolveClient(
    factory: Any,
    modelConfigs: Any,
    modelRouter: Any,
    session: AsyncSession | None,
    *,
    state: dict[str, Any],
    emit: Emit | None,
    sessionId: uuid.UUID,
    tokenUsage: Any = None,
    preferredModelId: int | None = None,
) -> tuple[Any, Any]:
    """解析本轮 LLM 客户端与其模型配置；无可用时显式降级并返回 `(None, None)`。

    `preferredModelId`（显式选定）透传直选；不可用时抛 `PreferredModelUnavailableError`。
    """
    config = await resolveModelConfig(
        modelConfigs,
        modelRouter,
        factory,
        session,
        question=str(state.get("question") or ""),
        sessionId=sessionId,
        tokenUsage=tokenUsage,
        preferredModelId=preferredModelId,
    )
    client = buildClient(factory, config)
    if client is None:
        await markLlmUnavailable(state, emit=emit, sessionId=sessionId)
        return None, None
    return client, config
