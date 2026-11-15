"""语义发布脚本（D12 工具）的退出码语义与备份范围。

为什么必须有这层单测
------------------
发布脚本是**写正式库**的唯一入口（除回填脚本外）。它的退出码是运维唯一的
判据：

- ``0`` = 发布成功；
- ``1`` = **发布闸门阻断**（内容不合规 ⇒ 不该发）；
- ``2`` = **前置失败**（参数 / 守卫 / 备份 / 版本冲突 / 状态机拒绝）。

退出码串味的后果很具体：并发冲突（`ConflictError`）若落进 ``1``，运维会
把它读成「闸门阻断」⇒ 去改治理内容，而真正的原因是有人同时在发布 ⇒
白排查一轮，还可能把不该发的内容改了。

「未捕获异常 ⇒ Python 退出码 1」这条默认行为会让**任何**异常都冒充
「闸门阻断」⇒ 状态机拒绝必须单独归到 ``2``。

另一条要钉死的是**备份范围 == 写入范围**：脚本会写 ``audit_log``（每次状态
迁移一行），只备份 ``semantic_release`` ⇒ 回滚点漏掉了脚本自己写下的行。
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from app.domain.exceptions import ConflictError, DomainError
from scripts import semantic_publish_snapshot as publishModule


class _FakeOperation:
    """`semanticReleaseService.createDraft` 的返回值替身。"""

    def __init__(
        self,
        *,
        releaseId: int = 1,
        blockers: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.release = type(
            "_Release",
            (),
            {
                "id": releaseId,
                "version": "v-test",
                "status": "ACTIVE",
                "manifest_hash": "hash",
                "manifest_json": {"classes": [], "joins": []},
            },
        )()
        self.blockers = blockers
        self.warnings = warnings


class _FakeReleaseService:
    """发布服务替身：三个方法各自可编排成功 / 抛异常。"""

    def __init__(
        self,
        *,
        createDraft: Any = None,
        markValidated: Any = None,
        activate: Any = None,
    ) -> None:
        self._createDraft = createDraft
        self._markValidated = markValidated
        self._activate = activate

    async def createDraft(self, *args: Any, **kwargs: Any) -> _FakeOperation:
        if self._createDraft is not None:
            return await self._createDraft(*args, **kwargs)
        return _FakeOperation()

    async def markValidated(self, *args: Any, **kwargs: Any) -> Any:
        if self._markValidated is not None:
            return await self._markValidated(*args, **kwargs)
        return type("_Validated", (), {"release": _FakeOperation().release})()

    async def activate(self, *args: Any, **kwargs: Any) -> Any:
        if self._activate is not None:
            return await self._activate(*args, **kwargs)
        return type("_Activated", (), {"release": _FakeOperation().release})()


def _args(**overrides: Any) -> argparse.Namespace:
    base = {
        "version": "v-test",
        "actor": "tester",
        "department": None,
        "note": None,
        "dry_run": False,
        "allow_prod_write": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _install(monkeypatch: pytest.MonkeyPatch, service: _FakeReleaseService) -> None:
    monkeypatch.setattr(
        "app.services.semantic_release_service.semanticReleaseService", service
    )


class Test退出码语义:
    async def test_发布成功退出0(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, _FakeReleaseService())

        code = await publishModule._publish(None, _args(), "postgresql://u@h/db")

        assert code == 0

    async def test_闸门阻断退出1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """内容不合规 ⇒ 阻断 ⇒ 退出码 1（不是 2：2 是前置失败）。"""
        _install(
            monkeypatch,
            _FakeReleaseService(
                createDraft=_async_return(_FakeOperation(blockers=("类缺属性",)))
            ),
        )

        code = await publishModule._publish(None, _args(), "postgresql://u@h/db")

        assert code == 1

    async def test_版本冲突退出2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """版本已存在 ⇒ 前置失败 ⇒ 退出码 2（不是 1：1 会被读成「闸门阻断」）。"""

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise ConflictError("版本已存在")

        _install(monkeypatch, _FakeReleaseService(createDraft=_boom))

        code = await publishModule._publish(None, _args(), "postgresql://u@h/db")

        assert code == 2

    async def test_起草阶段的领域失败退出2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise DomainError("体量超限")

        _install(monkeypatch, _FakeReleaseService(createDraft=_boom))

        code = await publishModule._publish(None, _args(), "postgresql://u@h/db")

        assert code == 2

    async def test_状态机拒绝退出2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """激活被状态机拒绝（非法转换 / 并发冲突）⇒ 前置失败 ⇒ 退出码 2。

        未捕获异常的默认退出码是 1 ⇒ 会冒充「闸门阻断」。
        """

        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise DomainError("状态机拒绝")

        _install(monkeypatch, _FakeReleaseService(activate=_boom))

        code = await publishModule._publish(None, _args(), "postgresql://u@h/db")

        assert code == 2


class Test备份范围:
    def test_备份范围覆盖脚本会写的全部表(self) -> None:
        """发布链路会写 ``semantic_release`` **和** ``audit_log``。

        只备份前者 ⇒ 回滚点漏掉脚本自己写下的审计行 ⇒
        「备份范围 == 写入范围」这条不变量不成立。
        """
        assert "semantic_release" in publishModule._BACKUP_TABLES
        assert "audit_log" in publishModule._BACKUP_TABLES

    def test_备份范围不含脚本不写的表(self) -> None:
        """范围越宽越好是错的：多备份一张不写的表 ⇒ 每次都白跑一次 dump。"""
        assert "ontology_metric" not in publishModule._BACKUP_TABLES
        assert "kpi_catalog" not in publishModule._BACKUP_TABLES


def _async_return(value: Any) -> Any:
    async def _inner(*args: Any, **kwargs: Any) -> Any:
        return value

    return _inner
