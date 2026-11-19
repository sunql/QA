"""recall_datasource_filter 纯函数单测（DB 路径的集成覆盖在
test_classify_and_recall.py；本文件只测无 DB 依赖的归一与三态降级）。"""

import logging
from types import SimpleNamespace

from app.services.recall_datasource_filter import (
    _tableKeys,
    applyDatasourceRecallFilter,
    filterClassesByDatasource,
    loadDatasourceTableSet,
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


# ---------------------------------------------------------------------------
# I3（2026-11-19 终审 Important-3）no_cache 必须有服务端日志
# ---------------------------------------------------------------------------


class _FakeScalarResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeScalarResult":
        return self

    def all(self) -> list:
        return list(self._rows)


class _FakeSession:
    """最小 session 替身：只提供 `execute(...).scalars().all()`。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def execute(self, stmt) -> _FakeScalarResult:  # noqa: ANN001, ANN202
        return _FakeScalarResult(self._rows)


async def test_load_table_set_warns_when_no_cache_rows(caplog) -> None:
    """无 schema_cache 行 ⇒ 返 None（fail-open）且**必须**有服务端 warning。

    终审 Important-3：原先「无行」「行存在但清单为空」两条分支都静默返 None ⇒
    披露了 no_cache，过滤 fail-open 空转，但服务端**零日志** ⇒ 运维看不到
    「数据源目录缺失」这一事实（fail-open 变成无声降级）。
    """
    with caplog.at_level(
        logging.WARNING, logger="app.services.recall_datasource_filter"
    ):
        tableSet = await loadDatasourceTableSet(_FakeSession([]), 42)
    assert tableSet is None
    assert any(
        "42" in record.getMessage() for record in caplog.records
    ), f"未记录 datasourceId：{[r.getMessage() for r in caplog.records]}"


async def test_load_table_set_warns_when_schema_data_empty(caplog) -> None:
    """schema_cache 行存在但清单为空（无表名）⇒ 同样 fail-open 且**必须**有日志。"""
    rows = [SimpleNamespace(schema_data=[])]
    with caplog.at_level(
        logging.WARNING, logger="app.services.recall_datasource_filter"
    ):
        tableSet = await loadDatasourceTableSet(_FakeSession(rows), 43)
    assert tableSet is None
    assert any(
        "43" in record.getMessage() for record in caplog.records
    ), f"未记录 datasourceId：{[r.getMessage() for r in caplog.records]}"


async def test_load_table_set_no_warning_when_cache_present(caplog) -> None:
    """正常路径（缓存有内容）**不**打 warning —— 日志只用于异常/空转场景。"""
    rows = [SimpleNamespace(schema_data=[{"table_name": "DIM_SUPPLIER"}])]
    with caplog.at_level(
        logging.WARNING, logger="app.services.recall_datasource_filter"
    ):
        tableSet = await loadDatasourceTableSet(_FakeSession(rows), 44)
    assert tableSet == {"DIM_SUPPLIER"}
    assert not [
        r for r in caplog.records
        if "44" in r.getMessage()
    ], "正常路径不应打 warning"


# ---------------------------------------------------------------------------
# M5（2026-11-19 终审必须合并前修）owner → 限定名 逻辑
# ---------------------------------------------------------------------------


def test_table_keys_include_bare_and_owner_qualified_names() -> None:
    """`_tableKeys` 的 owner 逻辑：裸名 ∪ owner 限定名（全大写）。

    生产是 Oracle **多 owner**（`schema_cache` 按 (datasource_id, schema_name)
    一行一 owner）⇒ 「多行取并集 + 裸名/限定名都认」是「不误杀」的核心口径，
    必须有单测钉住（原先零覆盖）。
    """
    keys = _tableKeys([
        {"table_name": "dim_supplier", "owner": "thbi"},   # 大小写归一
        {"table_name": "MAIN_DATA_FOO", "owner": "ZJTH"},   # owner 限定名
        {"table_name": "NO_OWNER_TABLE"},                   # 无 owner ⇒ 只有裸名
        {"owner": "ONLY_OWNER"},                            # 无表名 ⇒ 跳过
        {"table_name": "", "owner": "X"},                   # 空表名 ⇒ 跳过
    ])
    assert keys == {
        "DIM_SUPPLIER", "THBI.DIM_SUPPLIER",
        "MAIN_DATA_FOO", "ZJTH.MAIN_DATA_FOO",
        "NO_OWNER_TABLE",
    }
