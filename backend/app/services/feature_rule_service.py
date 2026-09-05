"""FeatureRule CRUD + outbox 审计 + 乐观锁（spec §9）。

所有写操作通过 OutboxService.enqueue 写审计，caller commit。
参照 feat-agent-tool-config-db 的 AgentToolConfigService 模式。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import (
    ConflictError,
    FeatureRuleNotFoundError,
    FeatureRuleReferencingError,
    FeatureRuleValidationError,
    FeatureRuleVersionConflictError,
)
from app.domain.models import FeatureRule, FeatureRuleThreshold
from app.domain.schemas import (
    FeatureRuleCreate,
    FeatureRuleThresholdCreate,
    FeatureRuleUpdate,
    _UnsetType,
)
from app.services.outbox_service import OutboxService

logger = logging.getLogger(__name__)


def _ruleRowToDict(row: FeatureRule) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "data_object": row.data_object,
        "data_layer": row.data_layer,
        "target_level": row.target_level,
        "feature_name": row.feature_name,
        "enabled": row.enabled,
        "priority": row.priority,
        "policy_description": row.policy_description,
        "version": row.version,
        "created_time": row.created_time.isoformat() if row.created_time else None,
        "updated_time": row.updated_time.isoformat() if row.updated_time else None,
        "thresholds": [
            {
                "severity": t.severity.value if hasattr(t.severity, "value") else t.severity,
                "operator": t.operator.value if hasattr(t.operator, "value") else t.operator,
                "threshold_value": str(t.threshold_value),
                "unit": t.unit,
                "threshold_order": t.threshold_order,
            }
            for t in (row.thresholds or [])
        ],
    }


def _seedThresholds(rule: FeatureRule, threshold_dicts: list[dict]) -> None:
    """Seed 专用：清空旧阈值 + 重建（spec §5.2 1:N）。"""
    rule.thresholds.clear()
    for td in threshold_dicts:
        rule.thresholds.append(FeatureRuleThreshold(
            severity=td["severity"].value if hasattr(td["severity"], "value") else td["severity"],
            operator=td["operator"].value if hasattr(td["operator"], "value") else td["operator"],
            threshold_value=td["threshold_value"],
            unit=td.get("unit"),
            threshold_order=td.get("threshold_order", 1),
        ))


class FeatureRuleService:
    def __init__(self, outbox: OutboxService | None = None) -> None:
        self._outbox = outbox or OutboxService()

    async def listRules(
        self, session: AsyncSession, *, enabledOnly: bool = False
    ) -> list[FeatureRule]:
        stmt = select(FeatureRule)
        if enabledOnly:
            stmt = stmt.where(FeatureRule.enabled.is_(True))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def getRule(self, session: AsyncSession, code: str) -> FeatureRule:
        result = await session.execute(
            select(FeatureRule).where(FeatureRule.code == code)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise FeatureRuleNotFoundError(f"feature_rule not found: {code}")
        return row

    async def createRule(
        self,
        session: AsyncSession,
        dto: FeatureRuleCreate,
        actor: CurrentUser,
    ) -> FeatureRule:
        row = FeatureRule(
            code=dto.code,
            data_object=dto.data_object,
            data_layer=dto.data_layer,
            target_level=dto.target_level,
            feature_name=dto.feature_name,
            enabled=dto.enabled,
            priority=dto.priority,
            policy_description=dto.policy_description,
            version=1,
        )
        for td in dto.thresholds:
            row.thresholds.append(FeatureRuleThreshold(
                severity=td.severity.value,
                operator=td.operator.value,
                threshold_value=td.threshold_value,
                unit=td.unit,
                threshold_order=td.threshold_order,
            ))
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as e:
            await session.rollback()
            if "uq_feature_rule_scope_code" in str(e.orig):
                raise ConflictError(
                    f"feature_rule code 已存在: {dto.code}"
                )
            raise

        await self._outbox.enqueue(
            session,
            event_type="feature_rule_created",
            entity_type="feature_rule",
            entity_id=row.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": None, "after": _ruleRowToDict(row)},
        )
        return row

    async def updateRule(
        self,
        session: AsyncSession,
        code: str,
        dto: FeatureRuleUpdate,
        actor: CurrentUser,
    ) -> FeatureRule:
        row = await self.getRule(session, code)
        before = _ruleRowToDict(row)
        if row.version != dto.version:
            raise FeatureRuleVersionConflictError(
                f"feature_rule version mismatch: current={row.version}, dto={dto.version}",
                current_version=row.version,
            )

        if not isinstance(dto.enabled, _UnsetType):
            row.enabled = dto.enabled
        if not isinstance(dto.priority, _UnsetType):
            row.priority = dto.priority
        if not isinstance(dto.policy_description, _UnsetType):
            row.policy_description = dto.policy_description
        if not isinstance(dto.thresholds, _UnsetType):
            row.thresholds.clear()
            for td in dto.thresholds:
                row.thresholds.append(FeatureRuleThreshold(
                    severity=td.severity.value,
                    operator=td.operator.value,
                    threshold_value=td.threshold_value,
                    unit=td.unit,
                    threshold_order=td.threshold_order,
                ))

        row.version += 1
        row.updated_time = datetime.now(timezone.utc)
        await session.flush()

        await self._outbox.enqueue(
            session,
            event_type="feature_rule_updated",
            entity_type="feature_rule",
            entity_id=row.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": before, "after": _ruleRowToDict(row)},
        )
        return row

    async def deleteRule(
        self,
        session: AsyncSession,
        code: str,
        actor: CurrentUser,
    ) -> None:
        row = await self.getRule(session, code)

        # 检查引用：AgentDefinition.tool_name 通过 code 引用（spec §9）。
        # v1：仅检查 AgentDefinition.tool_name == code 的引用。
        from app.domain.models import AgentDefinition
        referencing = (
            await session.execute(
                select(AgentDefinition.agent_code).where(
                    AgentDefinition.tool_name == code
                )
            )
        ).scalars().all()
        if referencing:
            raise FeatureRuleReferencingError(
                f"feature_rule 被 Agent 引用，无法删除: {code}",
                referencing=list(referencing),
            )

        before = _ruleRowToDict(row)
        deleted_id = row.id
        await session.delete(row)
        await session.flush()

        await self._outbox.enqueue(
            session,
            event_type="feature_rule_deleted",
            entity_type="feature_rule",
            entity_id=deleted_id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": before, "after": None},
        )

    async def toggleEnabled(
        self,
        session: AsyncSession,
        code: str,
        enabled: bool,
        actor: CurrentUser,
    ) -> FeatureRule:
        row = await self.getRule(session, code)
        before = _ruleRowToDict(row)
        row.enabled = enabled
        row.version += 1
        row.updated_time = datetime.now(timezone.utc)
        await session.flush()

        await self._outbox.enqueue(
            session,
            event_type="feature_rule_updated",
            entity_type="feature_rule",
            entity_id=row.id,
            actor=actor.userId,
            actor_departments=tuple(actor.departments or []),
            payload={"before": before, "after": _ruleRowToDict(row)},
        )
        return row

    async def upsertSeed(
        self,
        session: AsyncSession,
        code: str,
        fields: dict,
    ) -> FeatureRule:
        """Seed 专用 upsert（绕过 ACL + audit）。code 命中 → 全量更新元数据；未命中 → 插入。"""
        existing = (
            await session.execute(
                select(FeatureRule).where(FeatureRule.code == code)
            )
        ).scalar_one_or_none()
        if existing is None:
            row = FeatureRule(
                code=code,
                data_object=fields["data_object"],
                data_layer=fields["data_layer"],
                target_level=fields["target_level"],
                feature_name=fields["feature_name"],
                enabled=fields.get("enabled", True),
                priority=fields.get("priority", 100),
                policy_description=fields.get("policy_description"),
                version=1,
            )
            for td in fields["thresholds"]:
                row.thresholds.append(FeatureRuleThreshold(
                    severity=td["severity"].value if hasattr(td["severity"], "value") else td["severity"],
                    operator=td["operator"].value if hasattr(td["operator"], "value") else td["operator"],
                    threshold_value=td["threshold_value"],
                    unit=td.get("unit"),
                    threshold_order=td.get("threshold_order", 1),
                ))
            session.add(row)
            await session.flush()
            return row

        existing.data_object = fields["data_object"]
        existing.data_layer = fields["data_layer"]
        existing.target_level = fields["target_level"]
        existing.feature_name = fields["feature_name"]
        existing.enabled = fields.get("enabled", True)
        existing.priority = fields.get("priority", 100)
        existing.policy_description = fields.get("policy_description")
        _seedThresholds(existing, fields["thresholds"])
        existing.updated_time = datetime.now(timezone.utc)
        await session.flush()
        return existing
