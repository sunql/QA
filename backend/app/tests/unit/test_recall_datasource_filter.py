"""recall_datasource_filter 纯函数单测（DB 路径的集成覆盖在
test_classify_and_recall.py；本文件只测无 DB 依赖的归一与三态降级）。"""

from types import SimpleNamespace

from app.services.recall_datasource_filter import (
    applyDatasourceRecallFilter,
    filterClassesByDatasource,
)


def _cls(name: str, table: str | None) -> SimpleNamespace:
    return SimpleNamespace(class_name=name, source_table=table)


def test_filter_matches_bare_and_qualified_names_case_insensitive() -> None:
    tableSet = {"DIM_SUPPLIER", "THBI.MAIN_DATA_FOO"}
    kept, dropped = filterClassesByDatasource(
        [
            _cls("A", "dim_supplier"),          # 大小写归一命中
            _cls("B", "THBI.main_data_foo"),    # owner 限定名命中
            _cls("C", "MAIN_DATA_SUPPLIER"),    # 不在目录 ⇒ 被滤
            _cls("D", None),                    # 空 source_table ⇒ 保留
        ],
        tableSet,
    )
    assert [c.class_name for c in kept] == ["A", "B", "D"]
    assert dropped == ["MAIN_DATA_SUPPLIER"]


def test_filter_returns_new_list_without_mutation() -> None:
    classes = [_cls("A", "X1"), _cls("B", "X2")]
    kept, _ = filterClassesByDatasource(classes, {"X1"})
    assert [c.class_name for c in kept] == ["A"]
    assert [c.class_name for c in classes] == ["A", "B"]  # 入参不变


async def test_apply_skips_without_datasource() -> None:
    classes = [_cls("A", "WHATEVER")]
    kept, disclosure = await applyDatasourceRecallFilter(None, classes, datasourceId=None)
    assert [c.class_name for c in kept] == ["A"]  # 不过滤
    assert disclosure == {"datasourceFiltered": 0, "datasourceFilterSkipped": "no_datasource"}
