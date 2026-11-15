"""Agent 编排 run 的**语义发布钉住**（P3c D26/D27）单测。

run 创建时把「当前激活的语义发布」冻结进三个可空列
（`semantic_release_id` / `semantic_release_version` / `semantic_manifest_hash`）：

- **没有激活发布** ⇒ 三列全 NULL（旧 run 兼容；NULL = 未钉住 = 照旧）；
- **钉住了** ⇒ 运行期发布变更**不影响**该 run（重放一致的前提）；
- **语义面故障** ⇒ 降级为 NULL + 告警，**绝不挡 run 创建**（问答可用性优先）。

DDL 是迁移 `0145`（手写、只加法、可空列）⇒ ORM 与迁移的列注释逐字一致由
单元测试钉住（drift 工具比对注释，两边不一致 ⇒ `alembic check` 报漂移）。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.domain.agent_orchestration_models import AgentOrchestrationRun
from app.services.agent_orchestration_run_service import (
    _newQueuedRun,
    resolveSemanticPin,
)
from app.services.agent_orchestration_run_views import runToAuditDict


def _limits() -> dict:
    return {
        "max_iterations": 5,
        "max_tool_calls": 10,
        "max_cost_usd": 1.0,
        "timeout_seconds": 60,
    }


def _newRun(**kwargs) -> AgentOrchestrationRun:
    return _newQueuedRun(
        agent=SimpleNamespace(id=1, agent_code=" procurement_bot"),
        profile=SimpleNamespace(
            revision=3, instructions="inst", max_observation_chars=8000
        ),
        modelConfig=SimpleNamespace(id=2),
        snapshot=[],
        limits=_limits(),
        question="q",
        createdBy="tester",
        **kwargs,
    )


def _pin(releaseId: int = 7, version: str = "2026.11.09-1"):
    from app.services.agent_orchestration_run_service import SemanticReleasePin

    return SemanticReleasePin(
        releaseId=releaseId, version=version, manifestHash="a" * 64
    )


class _FakeRegistry:
    """语义 Registry 替身：可指定返回的快照 / 抛错。"""

    def __init__(self, snapshot=None, error: Exception | None = None) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls = 0

    async def getActive(self, session, *, domainCode=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.snapshot


def _snapshot(releaseId: int = 7):
    from app.services.semantic_registry_service import SemanticReleaseSnapshot

    return SemanticReleaseSnapshot(
        id=releaseId,
        version="2026.11.09-1",
        domain_code="PROCUREMENT",
        status="ACTIVE",
        schema_version="1.0",
        manifest_hash="b" * 64,
        activated_time=None,
        _manifestJson="{}",
    )


# =============================================================================
# D26：三列的写入与兼容
# =============================================================================


def test_钉住时三列被赋值() -> None:
    run = _newRun(semanticRelease=_pin())
    assert run.semantic_release_id == 7
    assert run.semantic_release_version == "2026.11.09-1"
    assert run.semantic_manifest_hash == "a" * 64


def test_不钉住时三列为空_旧_run_语义兼容() -> None:
    """没有激活发布 ⇒ 全 NULL；存量代码路径（不传 pin）行为不变。"""
    run = _newRun()
    assert run.semantic_release_id is None
    assert run.semantic_release_version is None
    assert run.semantic_manifest_hash is None


def test_ORM_三列声明_可空且注释与迁移一致() -> None:
    """注释必须与迁移 0145 的字面量**逐字一致**（alembic 比对列注释 ⇒ 漂移防线）。

    迁移文件按 0144 的既定惯例用**字面量**（迁移要冻结在时间里，不 import 业务代码）；
    这里把同样的字面量钉进单测 ⇒ ORM 侧漂移会被本用例抓住。
    """
    expected = {
        "semantic_release_id": "0145，P3c D26：本 run 钉住的语义发布 id（NULL = 未钉住 = 照旧取当前 ACTIVE）",
        "semantic_release_version": "0145，P3c D26：钉住发布的版本号快照（冗余，防发布行被删后 run 不可读）",
        "semantic_manifest_hash": "0145，P3c D26：钉住发布的 manifest 哈希快照（完整性核对用）",
    }
    for column, comment in expected.items():
        col = AgentOrchestrationRun.__table__.columns[column]
        assert col.nullable is True
        assert col.comment == comment


def test_审计快照含语义钉住三要素() -> None:
    """审计要能回答「这个 run 用的是哪版发布」⇒ 三列进审计 payload。"""
    run = _newRun(semanticRelease=_pin())
    audit = runToAuditDict(run)
    assert audit["semanticReleaseId"] == 7
    assert audit["semanticReleaseVersion"] == "2026.11.09-1"
    assert audit["semanticManifestHash"] == "a" * 64


def test_审计快照_未钉住时三要素为_None() -> None:
    audit = runToAuditDict(_newRun())
    assert audit["semanticReleaseId"] is None


# =============================================================================
# D27：钉住的解析与重放一致
# =============================================================================


async def test_有激活发布时解析出三要素() -> None:
    registry = _FakeRegistry(snapshot=_snapshot(7))
    pin = await resolveSemanticPin(session=None, registry=registry)
    assert pin is not None
    assert pin.releaseId == 7
    assert pin.version == "2026.11.09-1"
    assert pin.manifestHash == "b" * 64


async def test_无激活发布时返回_None() -> None:
    pin = await resolveSemanticPin(session=None, registry=_FakeRegistry(snapshot=None))
    assert pin is None


async def test_语义面故障时降级为_None_不挡创建() -> None:
    """Registry 炸了 ⇒ 钉不住就钉不住（记警），run 创建**不能**因此失败。"""
    pin = await resolveSemanticPin(
        session=None, registry=_FakeRegistry(error=RuntimeError("registry down"))
    )
    assert pin is None


async def test_重放一致_跨发布版本的两个_run_各自钉住() -> None:
    """发布从 7 切到 9：先建的 run 钉 7、后建的钉 9 ⇒ 重放各自稳定。"""
    registry = _FakeRegistry(snapshot=_snapshot(7))
    runA = _newRun(semanticRelease=await resolveSemanticPin(None, registry))

    registry.snapshot = _snapshot(9)
    runB = _newRun(semanticRelease=await resolveSemanticPin(None, registry))

    assert runA.semantic_release_id == 7
    assert runB.semantic_release_id == 9
    # A 不被 B 影响（钉住是创建时冻结的值，不是活引用）
    assert runA.semantic_release_version == "2026.11.09-1"
