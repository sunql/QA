"""统一 LLM 调用层（feat-wiki-knowledge，Phase 8 M2）。

所有知识学习机制（分类/关系/冲突/结构化）都经由本层调用模型，好处：

- **选模入口唯一**：按 ``llm_config.id`` 取配置，与 chat / DQ 生成同范式
- **降级可审计**：primary 失败自动切 fallback，结果里带 ``usedFallback``，
  回写 ``wiki_page.processing_model_id`` 后能看出「这条知识发生过降级」
- **计量不漏**：每次调用都写 ``wiki_token_usage``，成本不混进会话报表
- **失败语义统一**：无可用 client → ``LLMUnavailableError``（503），
  不让 SDK 的 ``Missing credentials`` 冒成 500

对齐的真实接口（勿凭记忆改）：

- ``ModelConfigService.get(session, configId)`` → 不存在抛 ``NotFoundError``
- ``createClient(config)`` → ``BaseLlmClient | None``；**无可用 key 时返回 None**
  （本地 Qwen 走 localhost:8888 且未配 ``QWEN_API_KEY`` 时同样返回 None，
  只有 ``provider='ollama'`` 才免 key）
- ``client.complete(messages, *, model, temperature, maxTokens, **kwargs)``
- ``LlmResponse`` 是**扁平字段**：``content`` / ``modelName`` /
  ``promptTokens`` / ``completionTokens`` / ``totalTokens``
  ——**没有** ``usage`` 对象、**没有** ``cost``，成本需按
  ``config.cost_per_1k_input/output`` 自行折算。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import LLMUnavailableError
from app.infrastructure.llm.base_client import LlmMessage, LlmResponse
from app.infrastructure.llm.factory import createClient
from app.services.messages_zh import (
    MSG_WIKI_LLM_EMPTY_RESPONSE,
    MSG_WIKI_LLM_MODEL_UNUSABLE,
    MSG_WIKI_LLM_PARSE_ERROR,
)
from app.services.model_config_service import ModelConfigService
from app.services.wiki_token_usage_service import WikiTokenUsageService

logger = logging.getLogger(__name__)

# LLM 常把 JSON 包在 ```json fence 里（deepseek 默认行为）。
# 与 data_quality_rule_llm_service / nl2sql_service 同一套正则。
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

# 单次学习的默认输出上限：分类/关系抽取都是短 JSON，2000 足够。
_DEFAULT_MAX_TOKENS = 2000


def _stripJsonFence(content: str) -> str:
    """剥掉 Markdown 代码围栏，返回其中的 JSON 文本。"""
    if not content:
        return content
    match = _JSON_FENCE_RE.search(content)
    return match.group(1).strip() if match else content.strip()


def _costFor(config: Any, promptTokens: int, completionTokens: int) -> Decimal:
    """按配置的千 token 单价折算本次成本（与 chat_service._costFor 同式）。"""
    inputCost = Decimal(promptTokens) * config.cost_per_1k_input / Decimal(1000)
    outputCost = Decimal(completionTokens) * config.cost_per_1k_output / Decimal(1000)
    return inputCost + outputCost


@dataclass(frozen=True)
class LearningLlmResult:
    """一次学习调用的结果 + 计量（不可变）。"""

    content: str
    modelConfigId: int
    modelName: str
    promptTokens: int
    completionTokens: int
    cost: Decimal
    usedFallback: bool = False


class LearningLLMInvoker:
    """按 id 选模 + fallback + 计量 的 LLM 调用器。

    每次调用构造一个实例即可（无跨请求状态）；``session`` 用于解析模型配置
    与写计量表。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        primaryModelId: int,
        fallbackModelId: int | None = None,
    ) -> None:
        self._session = session
        self._primaryModelId = primaryModelId
        self._fallbackModelId = fallbackModelId
        # 计量归属的导入任务；构造时还不知道 task.id（预检要先于建任务跑），
        # 故初值为 None，由 bindImportTask 在任务落库后补挂。
        self._importTaskId: int | None = None
        self._compileTaskId: int | None = None

    def bindImportTask(self, taskId: int) -> None:
        """补挂计量归属的任务 id。

        预检必须发生在建任务**之前**（否则失败会留下半截任务），而计量又要归到
        任务下，于是构造时还不知道 task.id —— 由调用方在任务落库后补挂一次。
        """
        self._importTaskId = taskId

    def bindCompileTask(self, taskId: int) -> None:
        self._compileTaskId = taskId

    async def preflight(self) -> None:
        """批量调用**前**的可用性预检：primary/fallback 都不可用则立刻抛 503。

        为什么值得单独一趟：单条调用失败时我们选择降级（分类是增强，不该
        拖垮知识入库）。但「模型压根没配凭据」这类**配置性错误**对每一条都会
        发生——若也按单条降级，用户会看到「导入成功」而所有条目都没分类，
        真相反被掩盖。所以配置性错误必须在动手前就炸出来。

        副作用：顺手把 client 塞进 factory 的进程内缓存，后续页面复用同一实例。
        """
        try:
            await self._resolveClient(self._primaryModelId)
            return
        except Exception as primaryError:
            if self._fallbackModelId is None:
                raise
            logger.warning(
                "learning LLM primary 预检失败，改用 fallback (primary=%s fallback=%s): %s",
                self._primaryModelId,
                self._fallbackModelId,
                primaryError,
            )
            await self._resolveClient(self._fallbackModelId)

    async def complete(
        self,
        *,
        systemPrompt: str,
        userPrompt: str,
        mechanism: str,
        purpose: str,
        maxTokens: int | None = None,
    ) -> LearningLlmResult:
        """调一次模型并计量；primary 失败且有 fallback 时自动降级重试一次。

        降级**只重试一次**，且不区分失败类型（配置缺失/无 key/网络错/超时）
        ——对调用方而言「primary 用不了」的处置都一样；fallback 也失败则把
        两个错误都带出来。
        """
        messages = [
            LlmMessage(role="system", content=systemPrompt),
            LlmMessage(role="user", content=userPrompt),
        ]

        try:
            response, config = await self._invokeOnce(
                self._primaryModelId, messages, maxTokens
            )
            usedFallback = False
        except Exception as primaryError:
            if self._fallbackModelId is None:
                raise
            logger.warning(
                "learning LLM primary 失败，降级到 fallback (primary=%s fallback=%s): %s",
                self._primaryModelId,
                self._fallbackModelId,
                primaryError,
            )
            try:
                response, config = await self._invokeOnce(
                    self._fallbackModelId, messages, maxTokens
                )
            except Exception as fallbackError:
                raise fallbackError from primaryError
            usedFallback = True

        cost = _costFor(config, response.promptTokens, response.completionTokens)
        await WikiTokenUsageService().record(
            self._session,
            mechanism=mechanism,
            modelConfigId=config.id,
            modelName=config.model_name,
            promptTokens=response.promptTokens,
            completionTokens=response.completionTokens,
            cost=cost,
            purpose=purpose,
            importTaskId=self._importTaskId,
            compileTaskId=self._compileTaskId,
        )

        return LearningLlmResult(
            content=response.content or "",
            modelConfigId=config.id,
            modelName=config.model_name,
            promptTokens=response.promptTokens,
            completionTokens=response.completionTokens,
            cost=cost,
            usedFallback=usedFallback,
        )

    async def completeJson(
        self, *, systemPrompt: str, userPrompt: str, mechanism: str, purpose: str,
        maxTokens: int | None = None,
    ) -> tuple[dict[str, Any], LearningLlmResult]:
        """调用并解析 JSON；解析失败抛 ``LLMUnavailableError``（503）。"""
        result = await self.complete(
            systemPrompt=systemPrompt,
            userPrompt=userPrompt,
            mechanism=mechanism,
            purpose=purpose,
            maxTokens=maxTokens,
        )
        if not result.content.strip():
            raise LLMUnavailableError(MSG_WIKI_LLM_EMPTY_RESPONSE)
        try:
            parsed = json.loads(_stripJsonFence(result.content))
        except json.JSONDecodeError as e:
            logger.warning("learning LLM 输出非 JSON (%s): %s", mechanism, e)
            raise LLMUnavailableError(MSG_WIKI_LLM_PARSE_ERROR) from e
        if not isinstance(parsed, dict):
            raise LLMUnavailableError(MSG_WIKI_LLM_PARSE_ERROR)
        return parsed, result

    async def _resolveClient(self, configId: int) -> tuple[Any, Any]:
        """解析配置 → 建 client，返回 ``(client, config)``。

        无可用凭据时抛 ``LLMUnavailableError``（503），而不是让 SDK 抛裸异常。
        """
        config = await ModelConfigService().get(self._session, configId)
        # is_active 必须在这里也拦一道：/wiki/import/models 对外宣称
        # usable = is_active 且凭据可用，若 execute 只验凭据，调用方可以
        # 传一个已停用但仍有 key 的模型 id 继续烧钱——接口自称的契约就假了。
        if not config.is_active:
            raise LLMUnavailableError(
                MSG_WIKI_LLM_MODEL_UNUSABLE.format(modelName=config.model_name)
            )
        client = createClient(config)
        if client is None:
            raise LLMUnavailableError(
                MSG_WIKI_LLM_MODEL_UNUSABLE.format(modelName=config.model_name)
            )
        return client, config

    async def _invokeOnce(
        self, configId: int, messages: list[LlmMessage], maxTokens: int | None
    ) -> tuple[LlmResponse, Any]:
        """解析配置 → 建 client → 调一次。任一环失败都抛异常给上层决定降级。"""
        client, config = await self._resolveClient(configId)
        effective_max = maxTokens or _DEFAULT_MAX_TOKENS
        logger.info("LLM invoke: configId=%s model=%s maxTokens=%s (requested=%s)", configId, config.model_name, effective_max, maxTokens)
        response = await client.complete(
            messages, maxTokens=effective_max
        )
        return response, config


__all__ = ["LearningLLMInvoker", "LearningLlmResult"]
