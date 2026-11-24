"""copilot 场景配置服务（P4）：读取（当前用户可见）+ 保存校验（fail-closed）。

设计文档：docs/superpowers/specs/2026-11-24-copilot-scenario-config-design.md
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.copilot_scenario_models import (
    RESULT_RENDERER_WHITELIST,
    SLOT_TYPE_WHITELIST,
    CopilotScenario,
)
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import AgentDefinition
from app.services.copilot_scenario_render import compileCodePattern


class CopilotScenarioService:
    """场景配置读取与保存校验。

    保存时 fail-closed 校验（把「工具不在目录」「Agent 停用」这类坑堵在配置时）：
    - Agent 必须存在且 ACTIVE；
    - ``result_renderer`` / ``slot_schema[].type`` 必须在白名单内。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def listEnabled(self) -> list[CopilotScenario]:
        """当前启用的场景（按 sort_order 升序）——卡片墙/工作区数据源。"""
        rows = (
            await self._session.execute(
                select(CopilotScenario)
                .where(CopilotScenario.enabled.is_(True))
                .order_by(CopilotScenario.sort_order, CopilotScenario.id)
            )
        ).scalars().all()
        return list(rows)

    async def getByCode(self, scenarioCode: str) -> CopilotScenario:
        row = (
            await self._session.execute(
                select(CopilotScenario).where(
                    CopilotScenario.scenario_code == scenarioCode
                )
            )
        ).scalars().first()
        if row is None or not row.enabled:
            raise NotFoundError(f"场景不存在或已停用：{scenarioCode}")
        return row

    async def save(
        self,
        *,
        scenarioCode: str,
        name: str,
        description: str,
        iconCode: str,
        agentCode: str,
        slotSchema: list[dict[str, Any]],
        questionTemplate: str,
        chips: list[dict[str, Any]],
        resultRenderer: str,
        requiredRole: str | None,
        sortOrder: int,
    ) -> CopilotScenario:
        await self._validate(
            agentCode=agentCode, slotSchema=slotSchema, resultRenderer=resultRenderer
        )
        existing = (
            await self._session.execute(
                select(CopilotScenario).where(
                    CopilotScenario.scenario_code == scenarioCode
                )
            )
        ).scalars().first()
        if existing is not None:
            existing.name = name
            existing.description = description
            existing.icon_code = iconCode
            existing.agent_code = agentCode
            existing.slot_schema = slotSchema
            existing.question_template = questionTemplate
            existing.chips = chips
            existing.result_renderer = resultRenderer
            existing.required_role = requiredRole
            existing.sort_order = sortOrder
            existing.enabled = True
            return existing

        row = CopilotScenario(
            scenario_code=scenarioCode,
            name=name,
            description=description,
            icon_code=iconCode,
            agent_code=agentCode,
            slot_schema=slotSchema,
            question_template=questionTemplate,
            chips=chips,
            result_renderer=resultRenderer,
            required_role=requiredRole,
            sort_order=sortOrder,
            enabled=True,
        )
        self._session.add(row)
        return row

    async def _validate(
        self, *, agentCode: str, slotSchema: list[dict[str, Any]], resultRenderer: str
    ) -> None:
        agent = (
            await self._session.execute(
                select(AgentDefinition).where(AgentDefinition.agent_code == agentCode)
            )
        ).scalars().first()
        if agent is None:
            raise NotFoundError(f"场景绑定的 Agent 不存在：{agentCode}")
        # AgentDefinition 无 enabled 列（可运行性由 profile/enabled 组合决定，
        # 运行时 fail loud：profile 未启用 ⇒ createRun 409）⇒ 此处只校验存在性。
        if resultRenderer not in RESULT_RENDERER_WHITELIST:
            raise ValidationError(
                f"result_renderer 不在白名单内：{resultRenderer}"
                f"（允许：{sorted(RESULT_RENDERER_WHITELIST)}）"
            )
        for slot in slotSchema:
            slotType = str(slot.get("type", ""))
            if slotType not in SLOT_TYPE_WHITELIST:
                raise ValidationError(
                    f"slot type 不在白名单内：{slotType}"
                    f"（允许：{sorted(SLOT_TYPE_WHITELIST)}）"
                )
            if not str(slot.get("key", "")).strip():
                raise ValidationError("slot 缺少 key")
            if not str(slot.get("label", "")).strip():
                raise ValidationError(f"slot {slot.get('key')} 缺少 label")
            # codePattern 必须是合法 regex（渲染时 re.error 走 fail-closed
            # 不命中——把配置错误拦在保存时，别等运行时静默失灵）
            codePattern = slot.get("codePattern")
            if codePattern:
                try:
                    compileCodePattern(str(codePattern))
                except re.error as exc:
                    raise ValidationError(
                        f"slot {slot.get('key')} codePattern 非法：{exc}"
                    ) from exc
