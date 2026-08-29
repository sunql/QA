"""本体表/列漂移交叉校验单元测试（2-4）。

纯逻辑（无 IO）：
- _validateOntologyAgainstSchema：本体 source_table/source_column 与 schema 缓存实际表/列比对
  - 裸表名（PRECEIPT）与带 owner 限定名（ZJTH.PRECEIPT）两种写法均可匹配
  - 表本身缺失时只报告表、不重复报告其列；列缺失仅针对"表存在但列缺失"
  - 无 source_table 的类 / 无 source_column 的属性自动忽略
  - 比较大小写不敏感（两侧 .upper()，避免大写 Oracle 本体对小写 PG/MySQL 缓存误报）
  - 多类共享同一缺失表/缺失列时去重，报告保留本体作者原文
- buildDriftWarning：漂移报告 → NL2SQL 提示告警文本（表漂移能告警），干净时返回空串；
  标识符渲染前净化（折叠换行 + 转义尖括号）

触 DB 的 API / 对话链路测试在 integration/test_ontology_drift_api.py。
"""

from __future__ import annotations

from app.domain.models import OntologyClass, OntologyProperty
from app.domain.schemas import MissingColumnRead, OntologyDriftReport
from app.services.schema_introspection_service import (
    _validateOntologyAgainstSchema,
    buildDriftWarning,
)


def _cls(name: str, table: str | None, props: list[dict] | None = None) -> OntologyClass:
    """构造本体类（可仅字段级，无需 DB）。"""
    return OntologyClass(
        class_name=name,
        source_table=table,
        properties=[OntologyProperty(**p) for p in (props or [])],
    )


def _schemaData(tables: list[dict]) -> list[dict]:
    """构造 schema 缓存原始 JSON（与 introspect 落库形状一致）。"""
    return [
        {
            "table_name": t["table_name"],
            "owner": t.get("owner", "ZJTH"),
            "columns": t.get("columns", []),
            "primary_keys": [],
            "foreign_keys": [],
        }
        for t in tables
    ]


def _cleanSchema() -> list[dict]:
    return _schemaData(
        [
            {
                "table_name": "PRECEIPT",
                "columns": [
                    {"column_name": "NAME", "data_type": "VARCHAR2", "nullable": False},
                    {"column_name": "QTY", "data_type": "NUMBER", "nullable": True},
                ],
            }
        ]
    )


class TestValidateOntologyAgainstSchema:
    def test_clean_references_report_no_drift(self) -> None:
        cls = _cls(
            "PRECEIPT",
            "ZJTH.PRECEIPT",
            [{"property_name": "NAME", "source_column": "NAME"},
             {"property_name": "QTY", "source_column": "QTY"}],
        )
        report = _validateOntologyAgainstSchema(_cleanSchema(), [cls], datasourceId=7)

        assert report.datasource_id == 7
        assert report.schema_cached is True
        assert report.has_drift is False
        assert report.missing_tables == []
        assert report.missing_columns == []
        assert report.checked_tables == 1

    def test_missing_table_is_reported(self) -> None:
        cls = _cls("Gone", "ZJTH.OLD_TABLE", [{"property_name": "ID", "source_column": "ID"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [cls], datasourceId=7)

        assert report.missing_tables == ["ZJTH.OLD_TABLE"]
        assert report.has_drift is True
        assert report.checked_tables == 1

    def test_bare_table_name_matches_cached_table(self) -> None:
        # source_table 无前缀（PG/MySQL 写法）：缓存表名即为裸名，应命中
        cls = _cls("PRECEIPT", "PRECEIPT", [{"property_name": "QTY", "source_column": "QTY"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [cls], datasourceId=7)

        assert report.has_drift is False

    def test_qualified_and_bare_forms_both_match(self) -> None:
        qualified = _cls("A", "ZJTH.PRECEIPT", [{"property_name": "Q", "source_column": "QTY"}])
        bare = _cls("B", "PRECEIPT", [{"property_name": "Q", "source_column": "QTY"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [qualified, bare], datasourceId=7)

        assert report.has_drift is False
        assert report.checked_tables == 2

    def test_missing_column_is_reported(self) -> None:
        cls = _cls(
            "PRECEIPT",
            "ZJTH.PRECEIPT",
            [{"property_name": "QTY", "source_column": "QTY"},
             {"property_name": "GONE", "source_column": "GONE_COL"}],
        )
        report = _validateOntologyAgainstSchema(_cleanSchema(), [cls], datasourceId=7)

        assert report.missing_tables == []
        assert report.missing_columns == [
            MissingColumnRead(table="ZJTH.PRECEIPT", column="GONE_COL")
        ]
        assert report.has_drift is True

    def test_missing_column_on_missing_table_only_reports_table(self) -> None:
        # 表本身缺失时不重复报告其列（列报告只针对"表存在但列缺失"）
        cls = _cls("Gone", "ZJTH.OLD_TABLE", [{"property_name": "C", "source_column": "ANY"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [cls], datasourceId=7)

        assert report.missing_tables == ["ZJTH.OLD_TABLE"]
        assert report.missing_columns == []

    def test_class_without_source_table_is_skipped(self) -> None:
        cls = _cls("NoTable", None, [{"property_name": "X", "source_column": "Y"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [cls], datasourceId=7)

        assert report.checked_tables == 0
        assert report.has_drift is False

    def test_property_without_source_column_is_skipped(self) -> None:
        cls = _cls("PRECEIPT", "ZJTH.PRECEIPT", [{"property_name": "NoCol"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [cls], datasourceId=7)

        assert report.checked_tables == 1
        assert report.has_drift is False

    def test_empty_schema_data_reports_tables_missing(self) -> None:
        cls = _cls("PRECEIPT", "ZJTH.PRECEIPT", [{"property_name": "Q", "source_column": "QTY"}])
        report = _validateOntologyAgainstSchema([], [cls], datasourceId=7)

        assert report.missing_tables == ["ZJTH.PRECEIPT"]
        assert report.has_drift is True

    def test_empty_classes_report_no_drift(self) -> None:
        report = _validateOntologyAgainstSchema(_cleanSchema(), [], datasourceId=7)

        assert report.checked_tables == 0
        assert report.has_drift is False

    def test_case_insensitive_table_match(self) -> None:
        # 共享大写 Oracle 本体指向小写缓存（PG/MySQL）时不误报：两侧 .upper() 比较
        cls = _cls("PRECEIPT", "preceipt", [{"property_name": "QTY", "source_column": "qty"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [cls], datasourceId=7)

        assert report.has_drift is False
        assert report.checked_tables == 1

    def test_case_insensitive_qualified_table_match(self) -> None:
        cls = _cls("PRECEIPT", "zjth.preceipt", [{"property_name": "QTY", "source_column": "QTY"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [cls], datasourceId=7)

        assert report.has_drift is False

    def test_case_insensitive_column_match(self) -> None:
        cls = _cls("PRECEIPT", "ZJTH.PRECEIPT", [{"property_name": "QTY", "source_column": "qty"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [cls], datasourceId=7)

        assert report.has_drift is False

    def test_case_insensitive_missing_table_still_reported(self) -> None:
        cls = _cls("Gone", "zjth.old_table", [{"property_name": "ID", "source_column": "ID"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [cls], datasourceId=7)

        assert report.missing_tables == ["zjth.old_table"]  # 保留本体作者原文
        assert report.has_drift is True

    def test_shared_missing_table_is_deduplicated(self) -> None:
        # 父子类共享同一缺失表：只报一次，不重复
        parent = _cls("P", "ZJTH.OLD_TABLE", [{"property_name": "ID", "source_column": "ID"}])
        child = _cls("C", "ZJTH.OLD_TABLE", [{"property_name": "NAME", "source_column": "NAME"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [parent, child], datasourceId=7)

        assert report.missing_tables == ["ZJTH.OLD_TABLE"]
        assert report.checked_tables == 2

    def test_shared_missing_column_is_deduplicated(self) -> None:
        # 两个类都引用同一缺失列：只报一次
        a = _cls("A", "ZJTH.PRECEIPT", [{"property_name": "G", "source_column": "GONE_COL"}])
        b = _cls("B", "ZJTH.PRECEIPT", [{"property_name": "G2", "source_column": "GONE_COL"}])
        report = _validateOntologyAgainstSchema(_cleanSchema(), [a, b], datasourceId=7)

        assert report.missing_columns == [
            MissingColumnRead(table="ZJTH.PRECEIPT", column="GONE_COL")
        ]
        assert report.has_drift is True


class TestBuildDriftWarning:
    def test_clean_report_yields_empty_warning(self) -> None:
        report = OntologyDriftReport(
            datasource_id=7, has_drift=False, schema_cached=True, checked_tables=1,
            missing_tables=[], missing_columns=[],
        )
        assert buildDriftWarning(report) == ""

    def test_missing_table_and_column_rendered(self) -> None:
        report = OntologyDriftReport(
            datasource_id=7,
            has_drift=True,
            schema_cached=True,
            checked_tables=2,
            missing_tables=["ZJTH.OLD_TABLE"],
            missing_columns=[MissingColumnRead(table="ZJTH.PRECEIPT", column="GONE_COL")],
        )
        warning = buildDriftWarning(report)

        assert "漂移" in warning or "不一致" in warning
        assert "ZJTH.OLD_TABLE" in warning
        assert "ZJTH.PRECEIPT.GONE_COL" in warning
        assert "请勿" in warning
        assert warning.count("\n- ") == 2  # 表与字段各一行

    def test_no_drift_warning_for_clean_chat_input(self) -> None:
        # 无漂移时 buildDriftWarning 返回空串，调用方（chat_service）据此不注入告警
        report = OntologyDriftReport(
            datasource_id=7, has_drift=False, schema_cached=True, checked_tables=0,
            missing_tables=[], missing_columns=[],
        )
        assert buildDriftWarning(report) == ""

    def test_identifier_with_newline_and_angle_brackets_is_sanitized(self) -> None:
        # 标识符渲染进 system prompt 前净化：折叠换行 + 转义尖括号（对齐 _sanitizeSchemaField）
        report = OntologyDriftReport(
            datasource_id=7,
            has_drift=True,
            schema_cached=True,
            checked_tables=1,
            missing_tables=["ZJTH.OLD\nIGNORE"],
            missing_columns=[MissingColumnRead(table="T", column="C<DROP>")],
        )
        warning = buildDriftWarning(report)

        assert "\nIGNORE" not in warning
        assert "OLD IGNORE" in warning
        assert "C&lt;DROP&gt;" in warning
        assert "<DROP>" not in warning
