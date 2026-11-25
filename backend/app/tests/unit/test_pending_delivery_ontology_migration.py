"""待到货迁本体 + THBI 的守卫：代码里不得再有源层表名与裸列名。"""

from __future__ import annotations

from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[3] / "app"
_MIGRATED_FILES = (
    _APP_DIR / "services" / "pending_delivery_query.py",
    _APP_DIR / "api" / "v1" / "procurement_analytics.py",
)


def _sourceText(fileName: str = "services/agent_tools_procurement.py") -> str:
    return (_APP_DIR / fileName).read_text(encoding="utf-8")


def test_no_zjth_schema_prefix_in_source() -> None:
    assert "ZJTH." not in _sourceText(), "待到货仍硬编码 ZJTH schema（违反 THBI 硬规则）"


def test_no_erp_raw_column_constants() -> None:
    text = _sourceText()
    for column in ("QTYSTU_0", "RCPQTYSTU_0", "POHNUM_0", "POPLIN_0", "BPSNUM_0"):
        assert column not in text, f"仍硬编码列名 {column}（应走本体绑定）"


def test_pending_table_constants_removed() -> None:
    text = _sourceText()
    for const in ("_PENDING_TABLE", "_PENDING_ITEM_TABLE",
                  "_PENDING_FACILITY_TABLE", "_PENDING_SUPPLIER_TABLE"):
        assert const not in text, f"{const} 应删除（改走本体绑定）"


def test_no_source_layer_hardcoding_in_migrated_modules() -> None:
    """守卫面覆盖拆出的实现模块与 API 层（表常量搬走不等于硬编码消失）。"""
    for path in _MIGRATED_FILES:
        text = path.read_text(encoding="utf-8")
        assert "ZJTH." not in text, f"{path.name} 仍硬编码 ZJTH schema"
        for column in ("QTYSTU_0", "RCPQTYSTU_0", "POHNUM_0", "POPLIN_0", "BPSNUM_0"):
            assert column not in text, f"{path.name} 仍硬编码列名 {column}"
