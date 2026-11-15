"""P3a D19：语义注册表 RAG 闸门（开关 + v1/v2 路由 + 降级口径）。

闸门是向量召回的**唯一出口**（与 P2 的 `semantic_schema_gate` 同款架构）：
调用方只说「要什么」，由闸门决定「读哪个集合、带不带过滤」。

三条不可让步的口径（本文件逐条钉死）
------------------------------------
1. **开关关 ⇒ 与今天逐字节一致**：读 v1 集合、不带任何过滤。
   过滤是 P3a 新增的**能力**，开关关 = 这项能力整体未启用（不是「启用但不生效」）。
   调用方在开关关时传了过滤器 ⇒ 记 warning + ``degradedReason`` 可观测
   （否则「以为在按域收窄、其实全量召回」是个静默的口径失真）。
2. **有作用域时，v2 不可用 ⇒ fail-closed（空结果），不许回落成无过滤的 v1**。
   回落 = 把越权行放进来 = 泄漏。宁可召回为空（上层有 fallback 路径），
   也不许用「更宽的答案」冒充「按权限过滤的答案」。
3. **无作用域时，v2 不可用 ⇒ 回落 v1**（此时 v1 与 v2 语义等价，回落不改变可见性）。

全部用 fake —— 不连 Milvus（本地 19530 是生产 Milvus，禁跑真连接）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.registry_filter import RegistryFilter
from app.services import semantic_registry_rag_gate as gate

_V1_HITS = [{"ontology_id": 1, "type": "class", "name": "供应商", "distance": 0.1}]


@pytest.fixture
def switchOn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gate, "getSettings", lambda: SimpleNamespace(enableSemanticRegistryRag=True)
    )


@pytest.fixture
def switchOff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gate, "getSettings", lambda: SimpleNamespace(enableSemanticRegistryRag=False)
    )


def _installV1(monkeypatch: pytest.MonkeyPatch, hits: list[dict[str, Any]] | None = None) -> list[str]:
    """把 v1 检索换成记录调用并返回固定结果的假实现。

    打在 ``gate.milvus``（= ``app.infrastructure.milvus_client`` 模块）上：
    闸门按**模块属性**调用 v1 检索 ⇒ 与生产代码里既有的 monkeypatch 点
    （``milvus_client.searchEmbeddingsByTypeRouted``）一致 ⇒ 老测试的
    替身仍然有效。
    """
    calls: list[str] = []

    def _fake(queryEmbedding, topK=5, typeFilter=None):
        calls.append(f"v1:{typeFilter}")
        return list(_V1_HITS if hits is None else hits)

    monkeypatch.setattr(gate.milvus, "searchEmbeddingsByTypeRouted", _fake)
    return calls


def _installV2(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exists: bool = True,
    hits: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> list[dict[str, Any] | None]:
    """把 v2 检索换成记录 expr 的假实现；返回每次调用的 expr 列表。"""
    exprs: list[dict[str, Any] | None] = []

    def _fakeHas(type_: str) -> bool:
        return exists

    def _fakeSearch(queryEmbedding, topK=5, typeFilter=None, expr=None):
        exprs.append(expr)
        if error is not None:
            raise error
        return list(hits if hits is not None else [])

    monkeypatch.setattr(gate, "hasRegistryV2Collection", _fakeHas)
    monkeypatch.setattr(gate, "searchRegistryV2", _fakeSearch)
    return exprs


# ---------------------------------------------------------------------------
# 1. 开关语义
# ---------------------------------------------------------------------------


class Test开关:
    def test_开关关闭时读v1且不带过滤(self, switchOff, monkeypatch) -> None:
        calls = _installV1(monkeypatch)
        exprs = _installV2(monkeypatch)

        result = gate.searchRegistryEmbeddings([0.1] * 4, topK=5)

        assert calls == ["v1:None"]
        assert exprs == []  # v2 一次都没碰
        assert result.hits == _V1_HITS
        assert result.usedRegistryV2 is False
        assert result.degradedReason is None

    def test_开关关闭时即便传了过滤器也不改读路径(self, switchOff, monkeypatch) -> None:
        """开关关 = 这项能力整体未启用 ⇒ 读路径与今天一致（不过滤、不碰 v2）。"""
        calls = _installV1(monkeypatch)
        exprs = _installV2(monkeypatch)

        result = gate.searchRegistryEmbeddings(
            [0.1] * 4,
            topK=5,
            registryFilter=RegistryFilter(domainCodes=("SUP",)),
        )

        assert calls == ["v1:None"]
        assert exprs == []
        assert result.usedRegistryV2 is False
        # 口径失真必须可观测：调用方以为按域收窄了，实际没有
        assert result.degradedReason == "switch_off_with_filter"


# ---------------------------------------------------------------------------
# 2. 开关开：路由
# ---------------------------------------------------------------------------


class Test路由:
    def test_开关开且无过滤器时读v2(self, switchOn, monkeypatch) -> None:
        calls = _installV1(monkeypatch)
        exprs = _installV2(monkeypatch, hits=_V1_HITS)

        result = gate.searchRegistryEmbeddings([0.1] * 4, topK=5)

        assert calls == []  # v1 一次都没碰
        assert exprs == [None]  # 无过滤器 ⇒ 无 expr
        assert result.usedRegistryV2 is True
        assert result.degradedReason is None

    def test_开关开且有过滤器时读v2并带表达式(self, switchOn, monkeypatch) -> None:
        _installV1(monkeypatch)
        exprs = _installV2(monkeypatch, hits=_V1_HITS)

        result = gate.searchRegistryEmbeddings(
            [0.1] * 4,
            topK=5,
            registryFilter=RegistryFilter(domainCodes=("SUP",)),
        )

        assert len(exprs) == 1
        assert exprs[0] == 'array_contains(domain_codes, "SUP")'
        assert result.usedRegistryV2 is True

    def test_类型过滤透传给v2(self, switchOn, monkeypatch) -> None:
        _installV1(monkeypatch)
        exprs = _installV2(monkeypatch)

        gate.searchRegistryEmbeddings([0.1] * 4, topK=5, typeFilter="class")

        assert exprs == [None]


# ---------------------------------------------------------------------------
# 3. 降级口径（安全关键）
# ---------------------------------------------------------------------------


class Test降级:
    def test_有作用域且v2缺失时fail_closed不回落到v1(self, switchOn, monkeypatch) -> None:
        """回落成无过滤的 v1 = 把越权行放进来 ⇒ 绝不允许。"""
        calls = _installV1(monkeypatch)
        _installV2(monkeypatch, exists=False)

        result = gate.searchRegistryEmbeddings(
            [0.1] * 4,
            topK=5,
            registryFilter=RegistryFilter(domainCodes=("SUP",)),
        )

        assert calls == []  # v1 一次都没碰
        assert result.hits == []
        assert result.usedRegistryV2 is False
        assert result.degradedReason == "v2_missing"

    def test_有作用域且v2检索异常时fail_closed(self, switchOn, monkeypatch) -> None:
        calls = _installV1(monkeypatch)
        _installV2(monkeypatch, error=RuntimeError("milvus down"))

        result = gate.searchRegistryEmbeddings(
            [0.1] * 4,
            topK=5,
            registryFilter=RegistryFilter(domainCodes=("SUP",)),
        )

        assert calls == []
        assert result.hits == []
        assert result.degradedReason == "v2_error"

    def test_无作用域且v2缺失时回落到v1(self, switchOn, monkeypatch) -> None:
        """无作用域 ⇒ v1 与 v2 语义等价 ⇒ 回落不改变可见性，可以回落。"""
        calls = _installV1(monkeypatch)
        _installV2(monkeypatch, exists=False)

        result = gate.searchRegistryEmbeddings([0.1] * 4, topK=5)

        assert calls == ["v1:None"]
        assert result.hits == _V1_HITS
        assert result.usedRegistryV2 is False
        assert result.degradedReason == "v2_missing"

    def test_无作用域且v2检索异常时回落到v1(self, switchOn, monkeypatch) -> None:
        calls = _installV1(monkeypatch)
        _installV2(monkeypatch, error=RuntimeError("milvus down"))

        result = gate.searchRegistryEmbeddings([0.1] * 4, topK=5)

        assert calls == ["v1:None"]
        assert result.hits == _V1_HITS
        assert result.degradedReason == "v2_error"

    def test_过滤器构造失败时fail_closed(self, switchOn, monkeypatch) -> None:
        """表达式构造不出来 ⇒ 不许拿「不过滤的答案」冒充「按权限过滤的答案」。"""
        calls = _installV1(monkeypatch)
        _installV2(monkeypatch)

        def _boom(_filter):
            raise ValueError("bad filter value")

        monkeypatch.setattr(gate, "buildRegistryFilterExpr", _boom)

        result = gate.searchRegistryEmbeddings(
            [0.1] * 4,
            topK=5,
            registryFilter=RegistryFilter(domainCodes=("SUP",)),
        )

        assert calls == []
        assert result.hits == []
        assert result.degradedReason == "filter_invalid"
