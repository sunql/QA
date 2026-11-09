"""语义发布 Registry（P1b / D8）的单测：缓存语义与只读边界。

本文件不连数据库（真实 PG 行为在 `integration/test_semantic_registry_service.py`），
只钉几件靠代码审查容易看漏、靠集成测试又太贵的事：

1. **缓存语义**：没有激活发布时也要缓存「空」——否则未发布状态下每次问答都打一次库；
   缓存按域作用域隔离；`invalidate()` 之后必须重新读库；TTL 兜底到期必须重新读库。
2. **缓存完整性**：调用方拿到的 manifest 必须是私有副本；失效与并发读之间的竞态
   不得把失效前的旧行回填进缓存。这两条都是 D7b 同类 HIGH 的镜像。
3. **只读边界（裁决一）**：Registry 只查 `semantic_release`，不得回查本体治理表、
   不得调用编译器。这类越界在功能上「看起来也对」，只有静态守卫能拦住。
4. **输入守卫**：非法参数在服务层就要拒绝，不把非法值带到 SQL。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.semantic_manifest_schema import manifestHash
from app.services import semantic_registry_service
from app.services.semantic_registry_service import (
    SemanticRegistry,
    SemanticReleaseIntegrityError,
    semanticRegistry,
)

_DEFAULT_MANIFEST: dict[str, Any] = {"schemaVersion": "v1"}


def _snapshotRow(
    *,
    rowId: int = 1,
    version: str = "2026.11.10-1",
    domainCode: str | None = None,
    schemaVersion: str = "v1",
    manifest: dict[str, Any] | None = None,
    manifestHash: str | None = None,
    activatedTime: datetime | None = None,
) -> SimpleNamespace:
    body = manifest if manifest is not None else dict(_DEFAULT_MANIFEST)
    return SimpleNamespace(
        id=rowId,
        version=version,
        domain_code=domainCode,
        status="ACTIVE",
        schema_version=schemaVersion,
        manifest_json=body,
        # 默认按真实规则算哈希：Registry 加载时会校验，假哈希会直接抛错。
        manifest_hash=manifestHash if manifestHash is not None else manifestHashOf(body),
        activated_time=activatedTime,
    )


def manifestHashOf(manifest: dict[str, Any]) -> str:
    return manifestHash(manifest)


def _firstSession(row: SimpleNamespace | None) -> AsyncMock:
    """`getActive` 用：session.execute(...).scalars().first() → row。"""
    result = MagicMock()
    result.scalars.return_value.first.return_value = row
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


def _listSession(rows: list[SimpleNamespace]) -> AsyncMock:
    """`listReleases` 用：session.execute(...).all() → rows。"""
    result = MagicMock()
    result.all.return_value = rows
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    return session


# ---------------------------------------------------------------------------
# 只读边界（裁决一）
# ---------------------------------------------------------------------------


def test_registry_module_never_reaches_governance_tables() -> None:
    """Registry 只读快照表。

    裁决一明确：`getActive()` 不得回查本体治理表，否则「治理表一改，历史发布内容
    就漂移」。这里用源码静态守卫钉住——运行时用例只能证明「当前查询没问题」，
    挡不住以后有人顺手加一条 join。
    """
    source = Path(semantic_registry_service.__file__).read_text(encoding="utf-8")

    for forbidden in (
        "OntologyClass",
        "OntologyProperty",
        "OntologyJoin",
        "OntologyMetric",
        "ontology_class",
        "ontology_property",
        "ontology_join",
        "ontology_metric",
        "semantic_compiler",
        "compileManifest",
    ):
        assert forbidden not in source, f"Registry 越界引用了 {forbidden}"


async def test_get_active_queries_only_the_snapshot_table() -> None:
    session = _firstSession(None)

    await SemanticRegistry().getActive(session)

    statement = str(session.execute.await_args.args[0])
    assert "semantic_release" in statement
    assert "ontology" not in statement


# ---------------------------------------------------------------------------
# 缓存语义
# ---------------------------------------------------------------------------


async def test_get_active_returns_none_without_active_release() -> None:
    """完成定义：无激活发布返回 None，不抛错。"""
    session = _firstSession(None)

    assert await SemanticRegistry().getActive(session) is None


async def test_empty_result_is_cached_too() -> None:
    """未发布状态下也不能每次都打库——否则功能一上线就是常态慢查询。"""
    registry = SemanticRegistry()
    session = _firstSession(None)

    assert await registry.getActive(session) is None
    assert await registry.getActive(session) is None
    session.execute.assert_awaited_once()


async def test_active_snapshot_is_cached_per_domain_scope() -> None:
    """全局快照与按域快照是不同缓存键，不能互相串。"""
    registry = SemanticRegistry()
    globalSession = _firstSession(_snapshotRow(rowId=1, domainCode=None))
    domainSession = _firstSession(_snapshotRow(rowId=2, domainCode="PROCUREMENT"))

    globalSnapshot = await registry.getActive(globalSession)
    domainSnapshot = await registry.getActive(domainSession, domainCode="PROCUREMENT")

    assert globalSnapshot is not None and globalSnapshot.id == 1
    assert domainSnapshot is not None and domainSnapshot.id == 2


async def test_second_read_of_same_scope_hits_cache() -> None:
    registry = SemanticRegistry()
    session = _firstSession(_snapshotRow())

    first = await registry.getActive(session)
    second = await registry.getActive(session)

    assert first is second
    session.execute.assert_awaited_once()


async def test_invalidate_forces_reload() -> None:
    registry = SemanticRegistry()
    session = _firstSession(_snapshotRow())

    await registry.getActive(session)
    registry.invalidate()
    await registry.getActive(session)

    assert session.execute.await_count == 2


async def test_cache_entry_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """本进程感知不到其他副本 / 手工改库，TTL 是最后的收敛兜底。"""
    registry = SemanticRegistry()
    session = _firstSession(_snapshotRow())

    clock = [1_000.0]
    monkeypatch.setattr(semantic_registry_service, "_monotonic", lambda: clock[0])

    await registry.getActive(session)
    clock[0] += semantic_registry_service._CACHE_TTL_SECONDS / 2
    await registry.getActive(session)
    session.execute.assert_awaited_once()

    clock[0] += semantic_registry_service._CACHE_TTL_SECONDS
    await registry.getActive(session)
    assert session.execute.await_count == 2


async def test_cache_stays_bounded_when_domain_codes_are_arbitrary() -> None:
    """安全阀：用任意域编码调用也不能把进程缓存撑成无界字典。"""
    registry = SemanticRegistry()
    cap = semantic_registry_service._MAX_CACHE_ENTRIES

    for index in range(cap + 5):
        await registry.getActive(_firstSession(_snapshotRow()), domainCode=f"D{index:03d}")

    assert len(registry._activeCache) <= cap


# ---------------------------------------------------------------------------
# 缓存完整性（D7b 同类 HIGH 的两个镜像）
# ---------------------------------------------------------------------------


async def test_each_call_gets_a_private_manifest_copy() -> None:
    """快照对象共享，但 manifest 必须是私有副本。

    `row.manifest_json` 是 ORM 实体上的可变 dict；如果直接塞进缓存指针，
    任何一个消费方原地改一下，全体请求 + ORM 实体都会看到被篡改的语义。
    """
    registry = SemanticRegistry()
    session = _firstSession(_snapshotRow(manifest={"schemaVersion": "v1", "classes": []}))

    first = await registry.getActive(session)
    assert first is not None
    first.manifest["classes"].append({"className": "被篡改"})

    second = await registry.getActive(session)
    assert second is not None
    assert second.manifest["classes"] == []

    # 也不得污染 ORM 实体本身。
    assert session.execute.return_value.scalars.return_value.first.return_value.manifest_json["classes"] == []


async def test_invalidate_during_read_does_not_repopulate_cache() -> None:
    """失效与并发读的竞态：失效前发出的读，不得把旧行回填进缓存。

    没有代数判断时，`getActive` 在 await 之后无条件写缓存——一次恰好在
    「激活事务提交并 invalidate」之前开始的读，会把旧快照永久钉在缓存里
    （直到下一次写操作），服务旧语义且没有任何自愈路径。
    """
    registry = SemanticRegistry()
    session = _firstSession(_snapshotRow(rowId=1))
    originalExecute = session.execute

    async def invalidateDuringRead(*args: Any, **kwargs: Any) -> Any:
        registry.invalidate()
        return await originalExecute(*args, **kwargs)

    session.execute = AsyncMock(side_effect=invalidateDuringRead)

    snapshot = await registry.getActive(session)
    assert snapshot is not None and snapshot.id == 1  # 本次请求仍返回读到的行

    # 若旧行走进了缓存，第二次调用会命中缓存、不再打库。
    await registry.getActive(session)
    assert session.execute.await_count == 2


async def test_tampered_snapshot_is_rejected_by_hash() -> None:
    """绕过发布路径写库的坏数据（内容 ≠ manifest_hash）不得被当语义使用。"""
    session = _firstSession(_snapshotRow(manifestHash="b" * 64))

    with pytest.raises(SemanticReleaseIntegrityError):
        await SemanticRegistry().getActive(session)


async def test_snapshot_is_frozen() -> None:
    """缓存对象共享给所有调用方，必须不可变，否则一个请求能改掉全局状态。"""
    registry = SemanticRegistry()
    session = _firstSession(_snapshotRow())

    snapshot = await registry.getActive(session)
    assert snapshot is not None
    with pytest.raises(FrozenInstanceError):
        snapshot.version = "tampered"  # type: ignore[misc]


def test_module_singleton_exists() -> None:
    """写路径（D9/D10）要能拿到同一个实例做失效。"""
    assert isinstance(semanticRegistry, SemanticRegistry)


# ---------------------------------------------------------------------------
# getById
# ---------------------------------------------------------------------------


async def test_get_by_id_returns_snapshot() -> None:
    registry = SemanticRegistry()
    session = AsyncMock()
    session.get = AsyncMock(
        return_value=_snapshotRow(rowId=7, activatedTime=datetime(2026, 11, 10, tzinfo=UTC))
    )

    snapshot = await registry.getById(session, 7)

    assert snapshot.id == 7
    assert snapshot.activated_time is not None


async def test_get_by_id_missing_raises_not_found() -> None:
    registry = SemanticRegistry()
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError) as caught:
        await registry.getById(session, 404)

    assert "404" in caught.value.message


async def test_get_by_id_rejects_non_positive_id() -> None:
    session = AsyncMock()

    with pytest.raises(ValidationError):
        await SemanticRegistry().getById(session, 0)
    session.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# listReleases 输入守卫
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, 201])
async def test_list_releases_rejects_out_of_range_limit(limit: int) -> None:
    with pytest.raises(ValidationError):
        await SemanticRegistry().listReleases(_listSession([]), limit=limit)


@pytest.mark.parametrize("offset", [-1, 1_000_001])
async def test_list_releases_rejects_out_of_range_offset(offset: int) -> None:
    with pytest.raises(ValidationError):
        await SemanticRegistry().listReleases(_listSession([]), offset=offset)


async def test_list_releases_rejects_unknown_status() -> None:
    """状态是枚举，非法值不该变成一条永远返回空集的 SQL。"""
    with pytest.raises(ValidationError):
        await SemanticRegistry().listReleases(_listSession([]), status="DELETED")


def test_summary_columns_exclude_the_manifest() -> None:
    """列表查询的列清单是白名单式写的，这里钉住它不夹带 ~1MB 的快照正文。"""
    assert "manifest_json" not in semantic_registry_service._SUMMARY_COLUMN_NAMES
    assert "id" in semantic_registry_service._SUMMARY_COLUMN_NAMES


@pytest.mark.parametrize("domainCode", ["", "X" * 21])
async def test_get_active_rejects_malformed_domain_code(domainCode: str) -> None:
    with pytest.raises(ValidationError):
        await SemanticRegistry().getActive(_firstSession(None), domainCode=domainCode)
