"""Phase 4.3 FeatureComputeService 单测（纯函数 + 只读护栏，不连库）。

覆盖：
1. _getKey 大小写不敏感（Oracle 大写列名 vs PG/fake 小写）
2. _normalize 空串归一 None
3. _parseRow：正常 / entity_key 缺失 / value+value_text 双空拒写
4. computeFeature 对写操作 SQL 抛 SqlSafetyError（_assert_read_only 前置）
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.exceptions import DataSourceError, SqlSafetyError, ValidationError
from app.services.feature_compute_service import (
    FeatureComputeService,
    _getKey,
    _normalize,
)


class TestGetKey:
    def test_lowercase_exact(self) -> None:
        assert _getKey({"entity_key": "Q630"}, "entity_key") == "Q630"

    def test_uppercase_oracle_column(self) -> None:
        assert _getKey({"ENTITY_KEY": "Q630"}, "entity_key") == "Q630"

    def test_mixed_case(self) -> None:
        assert _getKey({"Entity_Key": "Q630"}, "entity_key") == "Q630"

    def test_missing_returns_none(self) -> None:
        assert _getKey({"value": "1"}, "entity_key") is None


class TestNormalize:
    def test_none_passthrough(self) -> None:
        assert _normalize(None) is None

    def test_empty_string_to_none(self) -> None:
        assert _normalize("") is None

    def test_value_passthrough(self) -> None:
        assert _normalize("0.95") == "0.95"
        assert _normalize(0) == 0


class TestParseRow:
    def _svc(self) -> FeatureComputeService:
        return FeatureComputeService()

    def test_normal_numeric(self) -> None:
        entity_key, value, value_text = self._svc()._parseRow(
            {"entity_key": "Q630", "value": 0.95}
        )
        assert entity_key == "Q630"
        assert value == 0.95
        assert value_text is None

    def test_text_value(self) -> None:
        entity_key, value, value_text = self._svc()._parseRow(
            {"entity_key": "Q630", "value_text": "HIGH"}
        )
        assert value is None
        assert value_text == "HIGH"

    def test_missing_entity_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            self._svc()._parseRow({"value": 0.95})

    def test_both_null_raises(self) -> None:
        with pytest.raises(ValidationError):
            self._svc()._parseRow({"entity_key": "Q630", "value": None, "value_text": ""})

    def test_uppercase_oracle_row(self) -> None:
        entity_key, value, _ = self._svc()._parseRow(
            {"ENTITY_KEY": "Q630", "VALUE": 0.8}
        )
        assert entity_key == "Q630"
        assert value == 0.8

    def test_rejects_oversized_entity_key(self) -> None:
        with pytest.raises(ValidationError):
            self._svc()._parseRow({"entity_key": "K" * 101, "value": 1})

    def test_rejects_oversized_value_text(self) -> None:
        with pytest.raises(ValidationError):
            self._svc()._parseRow({"entity_key": "K", "value_text": "x" * 501})


class TestComputeFeatureReadOnlyGuard:
    async def test_write_sql_rejected_before_adapter(self) -> None:
        """calculation_logic 含写操作 → computeFeature 先 _assert_read_only 抛 SqlSafetyError。"""
        svc = FeatureComputeService()
        feature = SimpleNamespace(
            id=1,
            calculation_logic="DELETE FROM DWS_SUPPLIER_DELIVERY_MONTHLY",
        )
        with pytest.raises(SqlSafetyError):
            await svc.computeFeature(None, feature, None)


class TestComputeFeatureFailureHandling:
    """执行失败与行数上限（MEDIUM/H2）：不外泄驱动异常，超限拒写。"""

    async def test_execute_failure_wraps_as_data_source_error(self) -> None:
        svc = FeatureComputeService()
        feature = SimpleNamespace(id=1, datasource_id=4, calculation_logic="SELECT 1")

        class _BoomAdapter:
            async def execute_read_only(self, sql: str) -> list:
                raise RuntimeError("connection refused")

        with pytest.raises(DataSourceError):
            await svc.computeFeature(None, feature, _BoomAdapter())

    async def test_too_many_rows_rejected(self) -> None:
        svc = FeatureComputeService()
        feature = SimpleNamespace(id=1, datasource_id=4, calculation_logic="SELECT 1")

        class _ManyRowsAdapter:
            async def execute_read_only(self, sql: str) -> list:
                return [{"entity_key": f"K{i}", "value": i} for i in range(10_001)]

        with pytest.raises(ValidationError):
            await svc.computeFeature(None, feature, _ManyRowsAdapter())
