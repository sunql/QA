"""LocalImportService 单元测试（feat-ontology-import-comment）。

关注 _build_proposals 的 description 优先级：DB comment > LLM description > None。
不连真实 DB，只验证 proposal 装配逻辑；DB 落库与缓存走 integration 测试。
"""

from __future__ import annotations

import pytest

from app.domain.schemas import (
    ColumnSchemaRead,
    ImportRuleConfig,
    TableSchemaRead,
)
from app.services.import_llm_enhancer import (
    EnhancedColumn,
    EnhancedSchemaResult,
    EnhancedTable,
    FilterSuggestions,
)
from app.services.local_import_service import LocalImportService


def _table(
    name: str,
    columns: list[ColumnSchemaRead],
    comment: str | None = None,
) -> TableSchemaRead:
    return TableSchemaRead(
        table_name=name,
        owner="public",
        columns=columns,
        primary_keys=[],
        foreign_keys=[],
        comment=comment,
    )


def _col(name: str, data_type: str = "varchar", comment: str | None = None) -> ColumnSchemaRead:
    return ColumnSchemaRead(column_name=name, data_type=data_type, nullable=True, comment=comment)


def _enhanced(
    tables: list[EnhancedTable],
    filters: FilterSuggestions | None = None,
) -> EnhancedSchemaResult:
    return EnhancedSchemaResult(
        tables=tables,
        filter_suggestions=filters or FilterSuggestions(),
    )


def _empty_enhanced_for(_table_names: list[str]) -> EnhancedSchemaResult:
    """占位：不开 LLM 时 enhancer 兜底行为，留接口以备未来扩展（当前未引用）。"""
    return _enhanced([])


def _enhanced_matching(tables: list[TableSchemaRead]) -> EnhancedSchemaResult:
    """构造与原始表名/列名一一对应的空 LLM 增强（description/alias 均为 None）。"""
    out: list[EnhancedTable] = []
    for t in tables:
        out.append(
            EnhancedTable(
                name=t.table_name,
                columns=[EnhancedColumn(name=c.column_name) for c in t.columns],
            )
        )
    return _enhanced(out)


class TestBuildProposalsDescriptionPriority:
    """feat-ontology-import-comment：description 优先级矩阵。

    行为契约：description = LLM.description if 非空 else col.comment if 非空 else None
    类 description 同款：LLM.description > original.comment > None
    """

    @pytest.fixture
    def service(self) -> LocalImportService:
        # 真实构造：rule_engine/conflict_resolver/llm_enhancer/ontology_service 都用默认
        return LocalImportService()

    def test_db_column_comment_used_when_llm_description_absent(self, service: LocalImportService) -> None:
        """col.comment 非空 + LLM 无 description → property.description = col.comment"""
        table = _table(
            "PRECEIPT",
            [_col("PTHNUM_0", comment="收货单号"), _col("TOTQTY_0", comment="收货数量合计")],
        )
        enhanced = _enhanced_matching([table])

        classes, _, _ = service._build_proposals(enhanced, ImportRuleConfig(), [table])
        assert len(classes) == 1
        descriptions = [p.description for p in classes[0].properties]
        assert descriptions == ["收货单号", "收货数量合计"]

    def test_db_table_comment_used_when_llm_description_absent(self, service: LocalImportService) -> None:
        """original.comment 非空 + LLM 无 description → class.description = original.comment"""
        table = _table("PRECEIPT", [_col("PTHNUM_0")], comment="收货单主表")
        enhanced = _enhanced_matching([table])

        classes, _, _ = service._build_proposals(enhanced, ImportRuleConfig(), [table])
        assert classes[0].description == "收货单主表"

    def test_llm_description_takes_precedence_over_db_comment(self, service: LocalImportService) -> None:
        """LLM 开了 generate_descriptions 时 LLM 描述优先（更丰富/更准）。"""
        table = _table(
            "PRECEIPT",
            [_col("PTHNUM_0", comment="收货单号")],
            comment="收货单主表",
        )
        # LLM 给的属性描述与表描述都比 DB 自带的更细
        enhanced = _enhanced(
            [
                EnhancedTable(
                    name="PRECEIPT",
                    description="Sage X3 收货单主数据表（含预集成订单号、供应商信息、收货日期）",
                    columns=[
                        EnhancedColumn(
                            name="PTHNUM_0",
                            description="Sage X3 收货单主键，格式 PTH + YYMMDD + 4 位流水",
                        ),
                    ],
                )
            ]
        )

        classes, _, _ = service._build_proposals(enhanced, ImportRuleConfig(), [table])
        assert classes[0].description == (
            "Sage X3 收货单主数据表（含预集成订单号、供应商信息、收货日期）"
        )
        assert classes[0].properties[0].description == "Sage X3 收货单主键，格式 PTH + YYMMDD + 4 位流水"

    def test_no_llm_no_comment_yields_none_backward_compatible(
        self, service: LocalImportService
    ) -> None:
        """既无 LLM 描述也无 DB comment → description = None（向后兼容老导入路径）。"""
        table = _table("PRECEIPT", [_col("PTHNUM_0")])  # 无 comment
        enhanced = _enhanced_matching([table])  # 无 description

        classes, _, _ = service._build_proposals(enhanced, ImportRuleConfig(), [table])
        assert classes[0].description is None
        assert classes[0].properties[0].description is None

    def test_mixed_columns_each_resolved_independently(self, service: LocalImportService) -> None:
        """同一张表里：col1 有 LLM 描述、col2 无 LLM 但有 DB comment、col3 啥都没。

        验证：每列独立解析，互不影响（不是「表级一次性 fallback」）。
        """
        table = _table(
            "PRECEIPT",
            [
                _col("PTHNUM_0", comment="收货单号"),  # DB comment
                _col("BPSNUM_0", comment="供应商编号"),  # DB comment
                _col("TOTQTY_0"),  # 无 comment
            ],
        )
        # 只给 PTHNUM_0 加 LLM 描述，其它列 LLM 空
        enhanced = _enhanced(
            [
                EnhancedTable(
                    name="PRECEIPT",
                    columns=[
                        EnhancedColumn(
                            name="PTHNUM_0",
                            description="LLM 写的 PTHNUM_0（更细）",
                        ),
                        EnhancedColumn(name="BPSNUM_0"),
                        EnhancedColumn(name="TOTQTY_0"),
                    ],
                )
            ]
        )

        classes, _, _ = service._build_proposals(enhanced, ImportRuleConfig(), [table])
        props = classes[0].properties
        assert props[0].description == "LLM 写的 PTHNUM_0（更细）"  # LLM 优先
        assert props[1].description == "供应商编号"  # DB comment 兜底
        assert props[2].description is None  # 全空

    def test_db_comment_whitespace_only_treated_as_missing(self, service: LocalImportService) -> None:
        """col.comment 是空串 / None / 纯空白都视为无注释（与 _annotateComments 行为对齐）。"""
        table = _table(
            "PRECEIPT",
            [_col("PTHNUM_0")],
            comment="   ",
        )
        enhanced = _enhanced_matching([table])

        classes, _, _ = service._build_proposals(enhanced, ImportRuleConfig(), [table])
        # class.description 全空白 → 当作无
        assert classes[0].description is None
        # property.description 全空白 → 当作无
        assert classes[0].properties[0].description is None