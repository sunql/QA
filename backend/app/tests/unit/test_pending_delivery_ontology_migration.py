"""待到货迁本体 + THBI 的守卫：代码里不得再有源层表名与裸列名。

文本扫描是**粗粒度**守卫（注释里写到也会红）：需要解释“旧列名”时用描述性文字。
扫描面覆盖：承载 handler 的旧文件、拆出的实现模块、API 装配层 ——
把实现搬走不等于硬编码消失。
"""

from __future__ import annotations

from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[3] / "app"
_SOURCE_FILES = (
    _APP_DIR / "services" / "agent_tools_procurement.py",
    _APP_DIR / "services" / "pending_delivery_query.py",
    _APP_DIR / "services" / "pending_delivery_args.py",
    _APP_DIR / "services" / "entity_mapping_names.py",
    _APP_DIR / "api" / "v1" / "procurement_analytics.py",
)
#: ERP 原始列名（含明细/日期列）：出现即意味着绕过本体绑定。
_ERP_RAW_COLUMNS = (
    "QTYSTU_0",
    "RCPQTYSTU_0",
    "POHNUM_0",
    "POPLIN_0",
    "BPSNUM_0",
    "ITMREF_0",
    "EXTRCPDAT_0",
)
#: 源层裸表名（可带/不带 schema 前缀；`DIM_FACILITY` 含 FACILITY 故不列它）。
_RAW_TABLE_NAMES = ("PORDERQ", "ITMMASTER", "BPSUPPLIER")
_PENDING_TABLE_CONSTANTS = (
    "_PENDING_TABLE",
    "_PENDING_ITEM_TABLE",
    "_PENDING_FACILITY_TABLE",
    "_PENDING_SUPPLIER_TABLE",
)


def _sourceText(fileName: str = "services/agent_tools_procurement.py") -> str:
    return (_APP_DIR / fileName).read_text(encoding="utf-8")


def _migratedTexts() -> list[tuple[Path, str]]:
    return [(path, path.read_text(encoding="utf-8")) for path in _SOURCE_FILES]


def test_no_zjth_schema_prefix_in_source() -> None:
    for path, text in _migratedTexts():
        assert "ZJTH." not in text, f"{path.name} 仍硬编码 ZJTH schema（违反 THBI 硬规则）"


def test_no_source_layer_raw_table_names() -> None:
    """裸表名（含不带 schema 前缀的写法）也不得出现。"""
    for path, text in _migratedTexts():
        for table in _RAW_TABLE_NAMES:
            assert table not in text, f"{path.name} 仍引用源层表 {table}（应走本体绑定）"


def test_no_erp_raw_column_constants() -> None:
    for path, text in _migratedTexts():
        for column in _ERP_RAW_COLUMNS:
            assert column not in text, (
                f"{path.name} 仍硬编码列名 {column}（应走本体绑定）"
            )


def test_pending_table_constants_removed() -> None:
    """四个源层表常量必须删除（旧文件与拆出的新模块都扫）。"""
    for path, text in _migratedTexts():
        for const in _PENDING_TABLE_CONSTANTS:
            assert const not in text, f"{path.name} 仍有 {const}（改走本体绑定）"
