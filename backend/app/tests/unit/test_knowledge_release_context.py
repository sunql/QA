"""P4 发布面解析（`release_context`）—— **钉住发布的授权**单测。

钉住的是**授权不变量**（安全关键）：

- 客户端可以传 `releaseId` ⇒ 如果服务端不校验，这个参数就是**越权开关**：
  能钉住**别的域**的发布、或钉住**没激活**（DRAFT / VALIDATED / ARCHIVED）的发布
  ⇒ 绕过发布治理闸（发布面本来只有 ACTIVE 才对普通读侧可见）；
- 失败时**不许回显版本号 / 哈希**（降级态必须剥版本，见 `KnowledgeReleaseRef`）；
- 失败原因**必须统一** ⇒ 「不存在」与「存在但无权」若给不同文案，就是一个
  **存在性 oracle**（可枚举 release id）。

⚠️ 本文件在 `unit/` 下 ⇒ 允许新建（unit 分片按路径稳定哈希）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.domain.enums import SemanticReleaseStatus
from app.domain.exceptions import NotFoundError
from app.services.knowledge.messages import MSG_PINNED_RELEASE_UNAVAILABLE
from app.services.knowledge.release_context import resolveKnowledgeRelease
from app.services.semantic_registry_service import SemanticReleaseSnapshot


def _snapshot(
    *,
    releaseId: int = 7,
    version: str = "pilot-2026.11.16-1",
    domainCode: str | None = "PROCUREMENT",
    status: str = SemanticReleaseStatus.ACTIVE.value,
) -> SemanticReleaseSnapshot:
    """造一份发布快照（`SemanticReleaseSnapshot` 是 frozen dataclass）。"""
    return SemanticReleaseSnapshot(
        id=releaseId,
        version=version,
        domain_code=domainCode,
        status=status,
        schema_version="v1",
        manifest_hash="a" * 64,
        activated_time=None,
        _manifestJson=json.dumps({"classes": [], "businessObjects": []}),
    )


def _fakeRegistry(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: SemanticReleaseSnapshot | None,
    *,
    missing: bool = False,
) -> None:
    """把 `release_context` 里的 registry 换成假实现（不打库）。"""

    class _FakeRegistry:
        async def getById(self, session: Any, releaseId: int) -> SemanticReleaseSnapshot:
            if missing or snapshot is None:
                raise NotFoundError(f"语义发布不存在：{releaseId}")
            return snapshot

        async def getActive(self, session: Any, *, domainCode: str | None = None) -> Any:
            return snapshot

    monkeypatch.setattr(
        "app.services.knowledge.release_context.semanticRegistry", _FakeRegistry()
    )


async def test_pinnedActiveSameDomainReleaseIsUsable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """钉住**本域 ACTIVE** 发布 ⇒ 正常读到（非 admin 也可以）。"""
    _fakeRegistry(monkeypatch, _snapshot())

    resolved = await resolveKnowledgeRelease(
        None, domainCode="PROCUREMENT", releaseId=7, isAdmin=False
    )

    assert resolved.degraded is False
    assert resolved.release_id == 7
    assert resolved.version == "pilot-2026.11.16-1"


async def test_pinnedGlobalReleaseIsUsable(monkeypatch: pytest.MonkeyPatch) -> None:
    """钉住**全局发布**（`domain_code IS NULL`）⇒ 对任何域都成立。"""
    _fakeRegistry(monkeypatch, _snapshot(domainCode=None))

    resolved = await resolveKnowledgeRelease(
        None, domainCode="PROCUREMENT", releaseId=7, isAdmin=False
    )

    assert resolved.degraded is False
    assert resolved.release_id == 7


async def test_pinnedOtherDomainReleaseIsRejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """钉住**别的域**的发布 ⇒ 拒绝（哪怕它是 ACTIVE）。"""
    _fakeRegistry(monkeypatch, _snapshot(domainCode="QUALITY"))

    resolved = await resolveKnowledgeRelease(
        None, domainCode="PROCUREMENT", releaseId=7, isAdmin=False
    )

    assert resolved.degraded is True
    assert resolved.release_id is None
    assert resolved.version is None
    assert resolved.manifest_hash is None
    assert resolved.reason == MSG_PINNED_RELEASE_UNAVAILABLE


async def test_pinnedOtherDomainReleaseIsRejectedForAdminToo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """admin 也不能把**别的域**的发布钉到本域上（域一致性是正确性，不是权限）。"""
    _fakeRegistry(monkeypatch, _snapshot(domainCode="QUALITY"))

    resolved = await resolveKnowledgeRelease(
        None, domainCode="PROCUREMENT", releaseId=7, isAdmin=True
    )

    assert resolved.degraded is True
    assert resolved.version is None


@pytest.mark.parametrize(
    "status",
    [
        SemanticReleaseStatus.DRAFT.value,
        SemanticReleaseStatus.VALIDATED.value,
        SemanticReleaseStatus.ARCHIVED.value,
    ],
)
async def test_pinnedNonActiveReleaseIsRejectedForNonAdmin(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """非 admin 只能钉**已激活**的发布（DRAFT / VALIDATED / ARCHIVED 一律拒绝）。

    这条是「发布治理闸」在读侧的对齐：没走完治理流程的快照不对普通读侧可见。
    """
    _fakeRegistry(monkeypatch, _snapshot(status=status))

    resolved = await resolveKnowledgeRelease(
        None, domainCode="PROCUREMENT", releaseId=7, isAdmin=False
    )

    assert resolved.degraded is True
    assert resolved.version is None
    assert resolved.reason == MSG_PINNED_RELEASE_UNAVAILABLE


async def test_pinnedDraftReleaseIsAllowedForAdmin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """admin 可以钉**未激活**的发布（治理角色自查 / 预演）。"""
    _fakeRegistry(monkeypatch, _snapshot(status=SemanticReleaseStatus.DRAFT.value))

    resolved = await resolveKnowledgeRelease(
        None, domainCode="PROCUREMENT", releaseId=7, isAdmin=True
    )

    assert resolved.degraded is False
    assert resolved.release_id == 7


async def test_missingPinnedReleaseDegradesWithoutLeaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """钉住不存在的发布 ⇒ 降级，且**不挂版本号**。"""
    _fakeRegistry(monkeypatch, None, missing=True)

    resolved = await resolveKnowledgeRelease(
        None, domainCode="PROCUREMENT", releaseId=999, isAdmin=False
    )

    assert resolved.degraded is True
    assert resolved.manifest is None
    assert resolved.release_id is None
    assert resolved.version is None
    assert resolved.manifest_hash is None
    assert resolved.reason == MSG_PINNED_RELEASE_UNAVAILABLE


async def test_denialReasonDoesNotDistinguishMissingFromUnauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「不存在」与「存在但无权」**必须同一个文案** ⇒ 不给存在性 oracle。

    否则调用方可以二分枚举 release id（先试不存在的拿 A 文案，再试他域的拿 B 文案）。
    """
    _fakeRegistry(monkeypatch, None, missing=True)
    missing = await resolveKnowledgeRelease(
        None, domainCode="PROCUREMENT", releaseId=999, isAdmin=False
    )

    _fakeRegistry(monkeypatch, _snapshot(domainCode="QUALITY"))
    unauthorized = await resolveKnowledgeRelease(
        None, domainCode="PROCUREMENT", releaseId=7, isAdmin=False
    )

    assert missing.reason == unauthorized.reason


async def test_noActiveReleaseDegrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """没钉住且该域没有 ACTIVE ⇒ 降级（**不读活数据**）。"""

    class _FakeRegistry:
        async def getActive(self, session: Any, *, domainCode: str | None = None) -> Any:
            return None

    monkeypatch.setattr(
        "app.services.knowledge.release_context.semanticRegistry", _FakeRegistry()
    )

    resolved = await resolveKnowledgeRelease(None, domainCode="PROCUREMENT")

    assert resolved.degraded is True
    assert resolved.manifest is None
    assert resolved.version is None
    assert resolved.reason
