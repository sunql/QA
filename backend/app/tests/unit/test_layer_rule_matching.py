"""matchTableNameLayer + LayerRuleRegistry 单测（feat-data-domain-layer 方案 2）.

钉死：
- 前缀优先级：ODS_DIM_（pri 10）先于 ODS_（pri 60）命中
- pattern=None 手工层（FEATURE）不参与前缀匹配
- 未命中返回 None（unknownTables 语义：不吞表）
- registry：warmUp 后 rules() 走 DB 行；invalidate 后回退 DEFAULT_LAYER_RULES
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.services.data_layer_defaults import DEFAULT_LAYER_RULES, matchTableNameLayer
from app.services.layer_rule_registry import LayerRuleRegistry
from app.services.data_layer_defaults import LayerRule


# --- matchTableNameLayer -----------------------------------------------------


def test_ods_dim_prefix_wins_over_ods() -> None:
    """ODS_DIM_ 优先级 10 < ODS_ 优先级 60：字典维表归 ODS_DICT 而非 ODS_BUSINESS。"""
    assert matchTableNameLayer("ODS_DIM_SUPPLIER") == "ODS_DICT"


def test_ods_business_table() -> None:
    assert matchTableNameLayer("ODS_BPSNUM_0") == "ODS_BUSINESS"


def test_no_case_folding_in_pure_function() -> None:
    """纯函数不做大小写归一——归一口径由调用方决定（chat_recall 大写源表 /
    探测 API information_schema 原名 + upper）。"""
    assert matchTableNameLayer("ods_xxx") is None


def test_standard_layers() -> None:
    assert matchTableNameLayer("ADS_SUPPLIER_360") == "ADS"
    assert matchTableNameLayer("DWS_ORDER_DAY") == "DWS"
    assert matchTableNameLayer("DWD_PO_LINE") == "DWD"
    assert matchTableNameLayer("DIM_MATERIAL") == "DIM"


def test_unmatched_returns_none() -> None:
    """ERP 原生命名（如 X_0 后缀业务表）无前缀可判 → None，调用方计 unknownTables。"""
    assert matchTableNameLayer("BPSCUST") is None
    assert matchTableNameLayer("") is None


def test_manual_layer_never_prefix_matched() -> None:
    """FEATURE 层 pattern=NULL（手工声明）：任何表名都不可前缀命中它。"""
    for rule in DEFAULT_LAYER_RULES:
        if rule.layer == "FEATURE":
            assert rule.pattern is None
    assert matchTableNameLayer("FEATURE") is None


def test_custom_rules_override_default() -> None:
    rules = (LayerRule(layer="STAGING", pattern="STG_", priority=1),)
    assert matchTableNameLayer("STG_PO", rules) == "STAGING"


# --- LayerRuleRegistry -------------------------------------------------------


def test_unwarmed_registry_falls_back_to_defaults() -> None:
    """未 warmUp：rules() 返回默认规则（闭合 fallback），isWarmedUp()=False。"""
    registry = LayerRuleRegistry()
    assert registry.isWarmedUp() is False
    assert registry.rules() == DEFAULT_LAYER_RULES
    assert "FEATURE" in registry.distinctLayers()


def _fake_session(rows: list) -> AsyncMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


def test_warm_up_loads_db_rows_sorted_by_priority() -> None:
    import asyncio

    # 模拟 DB 行（乱序 + 含高优先级 ODS_DIM_）
    row = AsyncMock(layer="ODS_DICT", pattern="ODS_DIM_", priority=10, enabled=True)
    row2 = AsyncMock(layer="ODS_BUSINESS", pattern="ODS_", priority=60, enabled=True)
    registry = LayerRuleRegistry()
    session = _fake_session([row2, row])

    asyncio.run(registry.warmUp(session))
    assert registry.isWarmedUp() is True
    assert registry.rules() == (
        LayerRule(layer="ODS_DICT", pattern="ODS_DIM_", priority=10),
        LayerRule(layer="ODS_BUSINESS", pattern="ODS_", priority=60),
    )
    assert registry.distinctLayers() == ("ODS_DICT", "ODS_BUSINESS")


def test_invalidate_reverts_to_defaults() -> None:
    import asyncio

    registry = LayerRuleRegistry()
    session = _fake_session([])
    asyncio.run(registry.warmUp(session))
    registry.invalidate()
    assert registry.isWarmedUp() is False
    assert registry.rules() == DEFAULT_LAYER_RULES
