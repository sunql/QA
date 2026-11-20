"""C 方案（数据源入口 fail-closed）单元守卫。

停用源（``is_active=false``）禁止进入消费链路：
- ``ensureDatasourceActive``：活跃透传 / 停用抛 ``DataSourceInactiveError``
  （``ConflictError`` 子类 ⇒ ``statusForError`` 映射 409）；
- ``applyDatasourceRecallFilter`` L2 纵深：停用源在过滤层**直接抛错**
  （fail-closed，且判定在 schema_cache 读取**之前**）；活跃源保持
  ``no_cache`` fail-open（新源首次 introspect 前合法，不误伤）。
设计：Harness/changes/2026-11-20-ds2-fail-closed/design.md（用户拍板：
L2 本批做 / discovery_snapshot 不切 / ds2 是测试源，叙事从简）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.exceptions import (
    ConflictError,
    DataSourceInactiveError,
    statusForError,
)
from app.domain.models import DataSource
from app.services.datasource_service import ensureDatasourceActive
from app.services.recall_datasource_filter import applyDatasourceRecallFilter


def _ds(is_active: bool | None) -> DataSource:
    return DataSource(id=880001, name="ds", is_active=is_active)


def _cls(name: str, table: str) -> SimpleNamespace:
    return SimpleNamespace(class_name=name, source_table=table)


class _FakeSession:
    """session.get 返回预置 ds；execute（schema_cache 查询）记调用并回空集。"""

    def __init__(self, ds: object) -> None:
        self._ds = ds
        self.executed = False

    async def get(self, model: object, key: object) -> object:  # noqa: ANN001
        return self._ds

    async def execute(self, *args: object, **kwargs: object) -> object:  # noqa: ANN002, ANN003
        self.executed = True

        class _Result:
            def scalars(self) -> _Result:
                return self

            def all(self) -> list[object]:
                return []

        return _Result()


# ── L1：ensureDatasourceActive ────────────────────────────────────────────


def test_ensure_active_passes_through_active_datasource() -> None:
    # 活跃源 ⇒ 恒等透传（4 个活跃源消费路径零行为变化）
    assert ensureDatasourceActive(_ds(True)) is None


def test_ensure_active_passes_through_unsaved_object() -> None:
    # is_active=None = 内存构造对象（ORM default 只在 flush 时生效；DB 列
    # nullable=False ⇒ 生产行不可能是 None）⇒ 不拦（拦它 = 误判「未落库」为「停用」）
    assert ensureDatasourceActive(_ds(None)) is None


def test_ensure_active_raises_conflict_on_inactive() -> None:
    with pytest.raises(DataSourceInactiveError) as excInfo:
        ensureDatasourceActive(_ds(False))
    assert isinstance(excInfo.value, ConflictError), "409 经 ConflictError 继承获得"
    assert statusForError(excInfo.value) == 409
    assert "已停用" in str(excInfo.value), "消息必须可运维（区分 id 写错 vs 源停用）"


# ── L2：applyDatasourceRecallFilter 纵深 ─────────────────────────────────


@pytest.mark.asyncio
async def test_filter_raises_on_inactive_datasource_before_cache_read() -> None:
    session = _FakeSession(_ds(False))
    with pytest.raises(DataSourceInactiveError):
        await applyDatasourceRecallFilter(
            session, [_cls("A", "X")], datasourceId=880001,
        )
    assert session.executed is False, "停用判定必须先于 schema_cache 查询"


@pytest.mark.asyncio
async def test_filter_keeps_fail_open_for_active_datasource_without_cache() -> None:
    session = _FakeSession(_ds(True))
    kept, disclosure = await applyDatasourceRecallFilter(
        session, [_cls("A", "X")], datasourceId=880001,
    )
    assert disclosure["datasourceFilterSkipped"] == "no_cache"
    assert [c.class_name for c in kept] == ["A"], "活跃新源空 cache ⇒ 维持 fail-open"


@pytest.mark.asyncio
async def test_filter_keeps_no_cache_when_datasource_row_missing() -> None:
    # ds 行不存在（上游 get() 本应 404；此处纵深不扩权）⇒ 维持 no_cache 现状
    session = _FakeSession(None)
    kept, disclosure = await applyDatasourceRecallFilter(
        session, [_cls("A", "X")], datasourceId=880001,
    )
    assert disclosure["datasourceFilterSkipped"] == "no_cache"
    assert [c.class_name for c in kept] == ["A"]


@pytest.mark.asyncio
async def test_filter_no_datasource_branch_untouched() -> None:
    session = _FakeSession(_ds(False))
    kept, disclosure = await applyDatasourceRecallFilter(
        session, [_cls("A", "X")], datasourceId=None,
    )
    assert disclosure["datasourceFilterSkipped"] == "no_datasource"
    assert [c.class_name for c in kept] == ["A"], "未选源分支不读 ds（现状不变）"
