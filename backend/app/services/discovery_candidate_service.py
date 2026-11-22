"""发现候选生成（WP-ONT-AI A2 / G3-2）：**首次引入 LLM**。

输入 = 一次成功剖析的 `discovery_run`（已是 `SUCCEEDED`，带 `discovery_profile` 行）；
输出 = `discovery_candidate` / `discovery_finding` + 该 run 的 **LLM 计量**。

## 与纯函数的分工

- `discovery_candidate_prompt`：事实整理（有界）、prompt 构造、响应逐条校验；
- 本模块：解析模型配置 → 调一次 LLM → **先记账再解析** → 覆盖写候选/问题。

## 四条纪律

1. **钱先记账**：LLM 返回后立刻把 token/cost 累加到 `discovery_run` 并提交，
   再去解析。解析失败时钱已经花了 —— 不记账等于账实不符。
2. **候选步骤失败不改 run 状态**：剖析本身是成功的；候选生成失败只抛领域异常，
   run 仍是 `SUCCEEDED`（重跑即可，不产生「成功/失败」歧义）。
3. **证据完整率 100%**：纯函数已丢弃无证据/证据不实的条目；本模块只落合法条目。
4. **覆盖写**：同一个 run 重跑 ⇒ 先删该 run 的候选与问题再插入（一份 run 只有
   一套候选，历史由 audit / 重新剖析承担）。

## 守卫

发现域一旦出现 LLM 调用，F1 守卫（`test_wp_ont_ai_fusion_contract`）要求同文件出现
`prompt_tokens` / `completion_tokens` / `cost` —— 本模块即满足该断言的样本。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
from app.domain.discovery_pipeline_models import (
    DiscoveryCandidate,
    DiscoveryFinding,
    DiscoveryProfile,
    DiscoveryRun,
)
from app.domain.exceptions import (
    LlmClientError,
    NoAvailableModelError,
    NotFoundError,
    ValidationError,
)
from app.domain.models import LlmConfig
from app.infrastructure.llm.base_client import BaseLlmClient
from app.infrastructure.llm.factory import createClient
from app.services.discovery_candidate_prompt import (
    PROMPT_VERSION,
    CandidateFormatError,
    buildCandidateMessages,
    buildProfileFacts,
    parseCandidateResponse,
)

logger = logging.getLogger(__name__)

MSG_RUN_NOT_FOUND = "发现运行 {runId} 不存在"
MSG_RUN_NOT_SUCCEEDED = "发现运行 {runId} 状态为 {status}，只有 SUCCEEDED 的剖析才能生成候选"
MSG_NO_PROFILES = "发现运行 {runId} 没有剖析结果，先生成剖析再生成候选"
MSG_NO_MODEL = "没有可用的模型配置，无法生成候选"
MSG_CLIENT_UNAVAILABLE = "模型配置 {model} 未就绪（缺少 API Key 或密文不可用）"
MSG_BAD_RESPONSE = "模型回复无法解析为候选 JSON：{detail}"
MSG_SOURCE_MISSING = "发现源 {id} 不存在（运行记录与源不一致）"
MSG_SNAPSHOT_MISSING = "元数据快照 {id} 不存在（运行记录与快照不一致）"

_THOUSAND = Decimal("1000")


@dataclass(frozen=True, slots=True)
class CandidateRunResult:
    """一次候选生成的结果（不可变）。"""

    runId: int
    candidateCount: int
    findingCount: int
    skipped: tuple[str, ...]
    promptTokens: int
    completionTokens: int
    cost: Decimal


class DiscoveryCandidateService:
    """候选生成编排（IO 层）。"""

    def __init__(self, *, llmFactory=createClient) -> None:
        self._llmFactory = llmFactory

    async def generateCandidates(
        self,
        session: AsyncSession,
        *,
        runId: int,
        llmClient: BaseLlmClient | None = None,
        modelId: int | None = None,
    ) -> CandidateRunResult:
        """对一个成功的剖析 run 生成候选与问题。

        `llmClient`：测试/脚本注入；`None` ⇒ 按配置创建（无 key 时 503 语义）。
        `modelId`：显式钉模型（与「禁止后端覆盖用户 modelId」同口径：给了就只用它）。
        """
        run, source, snapshot = await self._loadRun(session, runId)
        profiles = await self._loadProfiles(session, runId)
        config = await self._resolveConfig(session, modelId)
        client = llmClient or self._createClient(config)
        messages = buildCandidateMessages(buildProfileFacts(profiles))

        try:
            response = await client.complete(
                messages,
                model=config.model_name,
                temperature=getattr(config, "temperature", None),
            )
        except LlmClientError:
            raise
        except Exception as exc:  # noqa: BLE001 - 外部服务异常统一翻译
            logger.error("候选生成 LLM 调用失败（run=%s）: %s", runId, exc, exc_info=True)
            raise LlmClientError(f"候选生成调用模型失败：{config.model_name}") from exc

        cost = _costOf(config, response.promptTokens, response.completionTokens)
        _recordMetering(run, response, cost)
        await session.commit()

        try:
            batch = parseCandidateResponse(response.content, facts=buildProfileFacts(profiles))
        except CandidateFormatError as exc:
            raise ValidationError(MSG_BAD_RESPONSE.format(detail=str(exc))) from exc

        await self._replaceRows(session, run, source, snapshot, batch)
        await session.commit()
        return CandidateRunResult(
            runId=run.id,
            candidateCount=len(batch.candidates),
            findingCount=len(batch.findings),
            skipped=batch.skipped,
            promptTokens=response.promptTokens,
            completionTokens=response.completionTokens,
            cost=cost,
        )

    # ------------------------------------------------------------ 内部

    async def _loadRun(
        self, session: AsyncSession, runId: int
    ) -> tuple[DiscoveryRun, DiscoverySource, MetadataSnapshot]:
        run = await session.get(DiscoveryRun, runId)
        if run is None:
            raise NotFoundError(MSG_RUN_NOT_FOUND.format(runId=runId))
        if run.status != "SUCCEEDED":
            raise ValidationError(
                MSG_RUN_NOT_SUCCEEDED.format(runId=runId, status=run.status)
            )
        source = await session.get(DiscoverySource, run.source_id)
        if source is None:
            raise ValidationError(MSG_SOURCE_MISSING.format(id=run.source_id))
        if run.snapshot_id is None:
            raise ValidationError(MSG_NO_PROFILES.format(runId=runId))
        snapshot = await session.get(MetadataSnapshot, run.snapshot_id)
        if snapshot is None:
            raise ValidationError(MSG_SNAPSHOT_MISSING.format(id=run.snapshot_id))
        return run, source, snapshot

    async def _loadProfiles(
        self, session: AsyncSession, runId: int
    ) -> Sequence[DiscoveryProfile]:
        rows = list(
            (
                await session.execute(
                    select(DiscoveryProfile)
                    .where(DiscoveryProfile.run_id == runId)
                    .order_by(DiscoveryProfile.table_name, DiscoveryProfile.column_name)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            raise ValidationError(MSG_NO_PROFILES.format(runId=runId))
        return rows

    async def _resolveConfig(
        self, session: AsyncSession, modelId: int | None
    ) -> LlmConfig:
        statement = select(LlmConfig).where(LlmConfig.is_active.is_(True))
        if modelId is not None:
            statement = statement.where(LlmConfig.id == modelId)
        statement = statement.order_by(LlmConfig.id)
        config = (await session.execute(statement.limit(1))).scalars().first()
        if config is None:
            raise NoAvailableModelError(MSG_NO_MODEL)
        return config

    def _createClient(self, config: LlmConfig) -> BaseLlmClient:
        client: Any = None
        try:
            client = self._llmFactory(config)
        except Exception as exc:  # noqa: BLE001 - 密文/配置异常统一翻译
            logger.error("候选生成模型客户端创建失败（config=%s）: %s", config.id, exc)
            raise LlmClientError(
                MSG_CLIENT_UNAVAILABLE.format(model=config.model_name)
            ) from exc
        if client is None:
            raise LlmClientError(MSG_CLIENT_UNAVAILABLE.format(model=config.model_name))
        return client

    async def _replaceRows(
        self,
        session: AsyncSession,
        run: DiscoveryRun,
        source: DiscoverySource,
        snapshot: MetadataSnapshot,
        batch: Any,
    ) -> None:
        await session.execute(
            delete(DiscoveryCandidate).where(DiscoveryCandidate.run_id == run.id)
        )
        await session.execute(
            delete(DiscoveryFinding).where(DiscoveryFinding.run_id == run.id)
        )
        session.add_all(
            [
                DiscoveryCandidate(
                    run_id=run.id,
                    source_id=source.id,
                    snapshot_id=snapshot.id,
                    candidate_type=draft.candidateType,
                    table_name=draft.table,
                    column_name=draft.column,
                    target_code=draft.targetCode,
                    payload=draft.payload,
                    evidence_refs=list(draft.evidence),
                    confidence=_asDecimal(draft.confidence),
                    model_version=run.model_version,
                    prompt_version=run.prompt_version,
                    status="CANDIDATE",
                )
                for draft in batch.candidates
            ]
        )
        session.add_all(
            [
                DiscoveryFinding(
                    run_id=run.id,
                    source_id=source.id,
                    snapshot_id=snapshot.id,
                    finding_type=draft.findingType,
                    table_name=draft.table,
                    column_name=draft.column,
                    severity=draft.severity,
                    status="OPEN",
                    detail=draft.detail,
                    evidence_refs=list(draft.evidence),
                )
                for draft in batch.findings
            ]
        )
        if batch.skipped:
            logger.warning(
                "候选生成丢弃 %d 条非法条目（run=%s）：%s",
                len(batch.skipped),
                run.id,
                "；".join(batch.skipped[:5]),
            )


# ---------------------------------------------------------------------------
# 内部：计量与小数
# ---------------------------------------------------------------------------


def _recordMetering(run: DiscoveryRun, response: Any, cost: Decimal) -> None:
    """token / cost / 模型版本 / prompt 版本累加（**解析前**调用）。"""
    run.prompt_tokens = int(run.prompt_tokens or 0) + int(response.promptTokens or 0)
    run.completion_tokens = int(run.completion_tokens or 0) + int(
        response.completionTokens or 0
    )
    run.cost = (Decimal(str(run.cost)) if run.cost is not None else Decimal("0")) + cost
    run.model_version = response.modelName or run.model_version
    run.prompt_version = PROMPT_VERSION


def _costOf(config: LlmConfig, promptTokens: int, completionTokens: int) -> Decimal:
    inputPrice = Decimal(str(config.cost_per_1k_input or 0))
    outputPrice = Decimal(str(config.cost_per_1k_output or 0))
    return (
        Decimal(int(promptTokens or 0)) / _THOUSAND * inputPrice
        + Decimal(int(completionTokens or 0)) / _THOUSAND * outputPrice
    )


def _asDecimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))
