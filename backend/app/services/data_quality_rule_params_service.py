"""DQ 规则结构化参数 service（feat-dq-rule-params v1 隔离命名空间）。

写入时编译；存量规则零影响（走既有 data_quality_service 路径）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import RuleType
from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import DataQualityRule
from app.domain.schemas_dq_rule_params import (
    DataQualityRuleParamsCreate, DataQualityRuleParamsRead,
    DataQualityRuleParamsUpdate,
)
from app.services.data_quality_evaluators.rule_params_compiler import compileRuleParams


def _toRead(rule: DataQualityRule) -> DataQualityRuleParamsRead:
    return DataQualityRuleParamsRead(
        id=rule.id,
        rule_code=rule.rule_code,
        rule_name=rule.rule_name,
        rule_type=RuleType(rule.rule_type),
        target_table=rule.target_table,
        target_column=rule.target_column,
        threshold=rule.threshold,
        severity=rule.severity,
        datasource_id=rule.datasource_id,
        rule_expression=rule.rule_expression,
        rule_params=rule.rule_params,
        config_mode="structured" if rule.rule_params else "custom",
    )


class DataQualityRuleParamsService:

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, *, datasource_id: int | None = None) -> list[DataQualityRuleParamsRead]:
        stmt = select(DataQualityRule)
        if datasource_id is not None:
            stmt = stmt.where(DataQualityRule.datasource_id == datasource_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_toRead(r) for r in rows]

    async def get(self, rule_id: int) -> DataQualityRuleParamsRead:
        rule = await self._session.get(DataQualityRule, rule_id)
        if rule is None:
            raise NotFoundError(f"规则 id={rule_id} 不存在")
        return _toRead(rule)

    async def create(self, dto: DataQualityRuleParamsCreate) -> DataQualityRuleParamsRead:
        compiled = (
            self._compileIfStructured(dto.rule_params, dto.rule_type, dto.target_column)
            if dto.rule_params else dto.rule_expression
        )
        rule = DataQualityRule(
            rule_code=dto.rule_code,
            rule_name=dto.rule_name,
            rule_type=dto.rule_type.value,
            target_table=dto.target_table,
            target_column=dto.target_column,
            threshold=dto.threshold,
            severity=dto.severity,
            datasource_id=dto.datasource_id,
            rule_params=dto.rule_params,
            rule_expression=compiled,
        )
        self._session.add(rule)
        await self._session.flush()
        await self._session.refresh(rule)
        await self._session.commit()
        return _toRead(rule)

    async def update(self, rule_id: int, dto: DataQualityRuleParamsUpdate) -> DataQualityRuleParamsRead:
        rule = await self._session.get(DataQualityRule, rule_id)
        if rule is None:
            raise NotFoundError(f"规则 id={rule_id} 不存在")
        if dto.rule_name is not None:
            rule.rule_name = dto.rule_name
        if dto.threshold is not None:
            rule.threshold = dto.threshold
        if dto.severity is not None:
            rule.severity = dto.severity
        if dto.target_column is not None:
            rule.target_column = dto.target_column
        if dto.rule_params is not None:
            rule.rule_params = dto.rule_params
            rule.rule_expression = self._compileIfStructured(
                dto.rule_params, RuleType(rule.rule_type), rule.target_column,
            )
        elif dto.rule_expression is not None:
            rule.rule_params = None
            rule.rule_expression = dto.rule_expression
        await self._session.flush()
        await self._session.refresh(rule)
        await self._session.commit()
        return _toRead(rule)

    async def delete(self, rule_id: int) -> None:
        rule = await self._session.get(DataQualityRule, rule_id)
        if rule is None:
            raise NotFoundError(f"规则 id={rule_id} 不存在")
        await self._session.delete(rule)
        await self._session.flush()
        await self._session.commit()

    @staticmethod
    def _compileIfStructured(params: dict, ruleType: RuleType, targetColumn: str | None) -> str:
        """结构化模式：params → 编译产物；存 rule_expression（evaluator 读这列）。"""
        synthetic = DataQualityRule(
            id=0, rule_code="x", rule_name="x", rule_type=ruleType.value,
            target_table="x", target_column=targetColumn,
        )
        try:
            return compileRuleParams(synthetic, params)
        except Exception as exc:
            raise ValidationError(f"rule_params 编译失败: {exc}") from exc


__all__ = ["DataQualityRuleParamsService"]
