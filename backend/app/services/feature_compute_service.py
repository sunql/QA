"""AI 特征计算服务（Phase 4.3）。

把 feature_definition.calculation_logic 按只读护栏执行到业务库，解析结果行，
幂等 upsert 到 feature_value（unique (feature_id, entity_key, valid_at) +
INSERT ... ON CONFLICT DO UPDATE）。

adapter 注入：_adapterProvider(datasourceId, ds) -> BusinessDbAdapter，默认
get_adapter；测试可注入 fake adapter（与 chat_service._adapterProvider 同模式）。
calculation_logic 双重只读校验：创建/更新时已验，computeFeature 再 _assert_read_only
防御性复验，然后 adapter.execute_read_only 内部再验一次。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import FeatureStatus
from app.domain.exceptions import DataSourceError, NotFoundError, ValidationError
from app.domain.models import DataSource, FeatureDefinition, FeatureValue
from app.domain.schemas import FeatureComputeResult
from app.infrastructure.business_db_pool import BusinessDbAdapter, _assert_read_only, get_adapter
from app.services.messages_zh import (
    MSG_FEATURE_DATASOURCE_NOT_FOUND,
    MSG_FEATURE_ENTITY_KEY_TOO_LONG,
    MSG_FEATURE_EXECUTE_FAILED,
    MSG_FEATURE_NOT_FOUND,
    MSG_FEATURE_TOO_MANY_ROWS,
    MSG_FEATURE_VALUE_EMPTY,
    MSG_FEATURE_VALUE_NO_ENTITY_KEY,
    MSG_FEATURE_VALUE_TEXT_TOO_LONG,
)

logger = logging.getLogger(__name__)

AdapterProvider = Callable[[int, DataSource], BusinessDbAdapter]

# 单特征单次计算允许写入的最大行数（防止误配 calculation_logic 导致全表倾泻）
_MAX_FEATURE_VALUE_ROWS = 10_000
# entity_key / value_text 列宽度（与 FeatureValue 模型 String 长度一致）
_ENTITY_KEY_MAX_LEN = 100
_VALUE_TEXT_MAX_LEN = 500


def _getKey(row: dict[str, Any], key: str) -> Any:
    """大小写不敏感取列值（Oracle 返回大写列名，PG/fake 返回小写）。"""
    for k, v in row.items():
        if k.lower() == key:
            return v
    return None


def _normalize(value: Any) -> Any:
    """空串 / None 归一为 None（Oracle 空串即 NULL）。"""
    if value is None or value == "":
        return None
    return value


class FeatureComputeService:
    """按 calculation_logic 计算特征值并幂等 upsert。"""

    def __init__(self, adapterProvider: AdapterProvider | None = None) -> None:
        self._adapterProvider = adapterProvider or get_adapter

    async def computeFeature(
        self,
        session: AsyncSession,
        feature: FeatureDefinition,
        adapter: BusinessDbAdapter,
        *,
        valid_at: date | None = None,
    ) -> int:
        """执行单条特征计算，解析结果行并 upsert，返回落库/覆盖的行数。

        计算时双重只读校验（创建时已验，这里防御性复验）；执行失败（连接/超时等）
        捕获后转 DataSourceError，避免把底层驱动异常泄漏给调用方。
        """
        _assert_read_only(feature.calculation_logic)
        try:
            rows = await adapter.execute_read_only(feature.calculation_logic)
        except Exception as exc:  # 驱动/网络异常不泄漏细节，统一转领域错误
            logger.warning(
                "Feature compute failed for id=%d datasource_id=%d: %s",
                feature.id,
                feature.datasource_id,
                exc,
            )
            raise DataSourceError(
                MSG_FEATURE_EXECUTE_FAILED.format(
                    id=feature.datasource_id, detail=str(exc)
                )
            ) from exc
        if len(rows) > _MAX_FEATURE_VALUE_ROWS:
            raise ValidationError(
                MSG_FEATURE_TOO_MANY_ROWS.format(
                    count=len(rows), limit=_MAX_FEATURE_VALUE_ROWS
                )
            )
        return await self._persistValues(session, feature.id, rows, valid_at or date.today())

    async def computeFeatureById(
        self,
        session: AsyncSession,
        feature_id: int,
        *,
        valid_at: date | None = None,
    ) -> int:
        """按 id 计算：加载定义 + 数据源 + adapter，再委托 computeFeature。"""
        feature = await session.get(FeatureDefinition, feature_id)
        if feature is None:
            raise NotFoundError(MSG_FEATURE_NOT_FOUND.format(id=feature_id))
        ds = await session.get(DataSource, feature.datasource_id)
        if ds is None:
            raise NotFoundError(MSG_FEATURE_DATASOURCE_NOT_FOUND.format(id=feature.datasource_id))
        adapter = self._adapterProvider(feature.datasource_id, ds)
        return await self.computeFeature(session, feature, adapter, valid_at=valid_at)

    async def computeAllEnabled(
        self,
        session: AsyncSession,
        *,
        valid_at: date | None = None,
    ) -> list[FeatureComputeResult]:
        """计算所有 is_enabled=true 且 status=ACTIVE 的特征，返回各特征落库行数。"""
        stmt = (
            select(FeatureDefinition)
            .where(
                FeatureDefinition.is_enabled.is_(True),
                FeatureDefinition.status == FeatureStatus.ACTIVE,
            )
            .order_by(FeatureDefinition.feature_name)
        )
        result = await session.execute(stmt)
        features = list(result.scalars().all())
        results: list[FeatureComputeResult] = []
        for feature in features:
            ds = await session.get(DataSource, feature.datasource_id)
            if ds is None:
                raise NotFoundError(MSG_FEATURE_DATASOURCE_NOT_FOUND.format(id=feature.datasource_id))
            adapter = self._adapterProvider(feature.datasource_id, ds)
            rows = await self.computeFeature(session, feature, adapter, valid_at=valid_at)
            results.append(FeatureComputeResult(feature_id=feature.id, rows=rows))
        return results

    async def _persistValues(
        self,
        session: AsyncSession,
        feature_id: int,
        rows: list[dict[str, Any]],
        valid_at: date,
    ) -> int:
        """解析结果行并 upsert 到 feature_value；空结果集返回 0。"""
        if not rows:
            return 0
        computed_at = datetime.now(UTC)
        for row in rows:
            entity_key, value, value_text = self._parseRow(row)
            stmt = pg_insert(FeatureValue).values(
                feature_id=feature_id,
                entity_key=entity_key,
                value=value,
                value_text=value_text,
                valid_at=valid_at,
                computed_at=computed_at,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["feature_id", "entity_key", "valid_at"],
                set_={
                    "value": stmt.excluded.value,
                    "value_text": stmt.excluded.value_text,
                    "computed_at": stmt.excluded.computed_at,
                },
            )
            await session.execute(stmt)
        await session.commit()
        return len(rows)

    def _parseRow(self, row: dict[str, Any]) -> tuple[str, Decimal | None, str | None]:
        """解析单行结果 → (entity_key, value, value_text)。

        - entity_key 缺失/空 → ValidationError（特征值无实体键无意义）
        - value 与 value_text 双空 → ValidationError（拒写）
        """
        entity_key = _normalize(_getKey(row, "entity_key"))
        if entity_key is None or not str(entity_key).strip():
            raise ValidationError(MSG_FEATURE_VALUE_NO_ENTITY_KEY)
        value = _normalize(_getKey(row, "value"))
        value_text = _normalize(_getKey(row, "value_text"))
        if value is None and value_text is None:
            raise ValidationError(MSG_FEATURE_VALUE_EMPTY)

        key = str(entity_key).strip()
        if len(key) > _ENTITY_KEY_MAX_LEN:
            raise ValidationError(
                MSG_FEATURE_ENTITY_KEY_TOO_LONG.format(length=len(key))
            )
        if value_text is not None and len(str(value_text)) > _VALUE_TEXT_MAX_LEN:
            raise ValidationError(
                MSG_FEATURE_VALUE_TEXT_TOO_LONG.format(length=len(str(value_text)))
            )
        return key, value, value_text
