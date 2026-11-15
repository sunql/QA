"""P3a D21R2：过滤维度的**硬化**（作用域语义反转 + 字面量校验）。

背景（安全评审 HIGH-1）
----------------------
上一轮（D21R）引入的 ``isScopeBound`` 只是把「空作用域无法表达」变成
「可以用两种方式表达」，而**默认那一种仍然是不安全的**：

    RegistryFilter(domainCodes=())      # 忘了写 isScopeBound=True

⇒ ``hasScope() == False`` ⇒ 闸门认为「本轮无作用域」⇒ 返回**全量可见**。
恰好在「本该什么都看不到」的一轮变成全量可见 ⇒ 越权。

本轮裁决
--------
**过滤器的「存在」本身就是作用域声明**：

- ``registryFilter=None`` ⇒ 本轮**无作用域**（不收窄；v1/v2 语义等价）；
- 任何 ``RegistryFilter`` 实例 ⇒ **已声明作用域**；各维度取值为空 ⇒ deny-all。

⇒ 「忘记写标志位」不再是可能状态；不需要额外的布尔位，也就没有忘记写的可能。

字面量校验（MEDIUM-1/2、LOW-1）
------------------------------
过滤侧与写侧曾各写一套归一 ⇒ 不对称：

- 写侧 ``_codeList`` 会 strip / 丢空串 / 截断 64 字；
- 过滤侧不 strip、不丢空 ⇒ ``entity_type == ""`` 命中**所有非 BO 头类**
  （空串是存储层合法哨兵值）⇒ 调用方传一个空实体类型，得到的是「放大」而不是「收窄」；
- 写侧截断到 64 字、过滤侧不截断 ⇒ 调用方持**被截断的前缀**时，
  可能命中「真实码更长」的域行 ⇒ 跨域可见（泄漏方向）。

⇒ 过滤侧与写侧共用同一套归一，并对**空 / 超长 / 含控制字符**的字面量
**fail fast 拒绝**（闸门把异常转成空结果 ⇒ fail-closed）。

全部纯对象 —— 不连 Milvus、不连 PG。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.registry_filter import RegistryFilter
from app.infrastructure.milvus_registry_v2 import buildRegistryFilterExpr
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


def _installV1(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _fake(queryEmbedding, topK=5, typeFilter=None):
        calls.append(f"v1:{typeFilter}")
        return list(_V1_HITS)

    monkeypatch.setattr(gate.milvus, "searchEmbeddingsByTypeRouted", _fake)
    return calls


def _installV2(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any] | None]:
    exprs: list[dict[str, Any] | None] = []

    def _fakeSearch(queryEmbedding, topK=5, typeFilter=None, expr=None):
        exprs.append(expr)
        return list(_V1_HITS)

    monkeypatch.setattr(gate, "hasRegistryV2Collection", lambda type_: True)
    monkeypatch.setattr(gate, "searchRegistryV2", _fakeSearch)
    return exprs


# ---------------------------------------------------------------------------
# 1. 作用域语义：过滤器的「存在」= 已声明作用域
# ---------------------------------------------------------------------------


class Test作用域语义:
    def test_无过滤器时仍是全量可见(self, switchOn, monkeypatch) -> None:
        """反向守卫：别把口径收得过紧 —— 没有任何作用域声明时行为不变。"""
        calls = _installV1(monkeypatch)
        exprs = _installV2(monkeypatch)

        result = gate.searchRegistryEmbeddings([0.1] * 4, topK=5)

        assert calls == []
        assert exprs == [None]
        assert result.hits == _V1_HITS
        assert result.degradedReason is None

    def test_空过滤器在开关开时deny_all(self, switchOn, monkeypatch) -> None:
        """``RegistryFilter()`` = 声明了作用域但各维度取值为空 ⇒ 什么都不许看到。"""
        calls = _installV1(monkeypatch)
        exprs = _installV2(monkeypatch)

        result = gate.searchRegistryEmbeddings(
            [0.1] * 4, topK=5, registryFilter=RegistryFilter()
        )

        assert calls == []
        assert exprs == []
        assert result.hits == []
        assert result.degradedReason == "scope_denied"

    def test_空过滤器在开关关时fail_closed(self, switchOff, monkeypatch) -> None:
        """开关关 = 没有能力施加收窄 ⇒ fail-closed，不回落成不过滤的 v1。"""
        calls = _installV1(monkeypatch)
        exprs = _installV2(monkeypatch)

        result = gate.searchRegistryEmbeddings(
            [0.1] * 4, topK=5, registryFilter=RegistryFilter()
        )

        assert calls == []
        assert exprs == []
        assert result.hits == []
        assert result.degradedReason == "switch_off_denied"

    def test_半声明的空维度不再是全量可见(self, switchOn, monkeypatch) -> None:
        """回归守卫：``domainCodes=()``（没写任何标志位）也必须 deny-all。

        这是安全评审 HIGH-1 的原始场景 —— 旧语义下这里是**全量可见**。
        """
        calls = _installV1(monkeypatch)
        exprs = _installV2(monkeypatch)

        result = gate.searchRegistryEmbeddings(
            [0.1] * 4, topK=5, registryFilter=RegistryFilter(domainCodes=())
        )

        assert calls == []
        assert exprs == []
        assert result.hits == []
        assert result.degradedReason == "scope_denied"

    def test_过滤器不再接受作用域标志位(self) -> None:
        """``isScopeBound`` 已下线 —— 留着会让人以为「不写标志位 = 无作用域」。"""
        with pytest.raises(TypeError):
            RegistryFilter(isScopeBound=True)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 2. 字面量校验（与写侧同一套归一）
# ---------------------------------------------------------------------------


class Test字面量校验:
    def test_空字面量被拒绝(self) -> None:
        """空串是存储层的合法哨兵值（``entity_type=""`` = 非 BO 头类）⇒ 放行等于「放大」。"""
        with pytest.raises(ValueError):
            buildRegistryFilterExpr(RegistryFilter(entityTypes=("",)))

    def test_空白字面量被拒绝(self) -> None:
        with pytest.raises(ValueError):
            buildRegistryFilterExpr(RegistryFilter(domainCodes=("   ",)))

    def test_超长字面量被拒绝(self) -> None:
        """写侧会静默截断到 64 字 ⇒ 过滤侧持前缀可能跨域命中 ⇒ 直接拒绝。"""
        with pytest.raises(ValueError):
            buildRegistryFilterExpr(RegistryFilter(domainCodes=("X" * 65,)))

    def test_实体类型超长被拒绝(self) -> None:
        """``entity_type`` 列宽只有 32 字。"""
        with pytest.raises(ValueError):
            buildRegistryFilterExpr(RegistryFilter(entityTypes=("X" * 33,)))

    def test_控制字符被拒绝(self) -> None:
        """裸 LF/CR 会让 Milvus 表达式解析失败（实测 token recognition error）⇒ 杀掉整域召回。"""
        with pytest.raises(ValueError):
            buildRegistryFilterExpr(RegistryFilter(domainCodes=("SUP\nX",)))

    def test_非字符串字面量被拒绝(self) -> None:
        with pytest.raises(ValueError):
            buildRegistryFilterExpr(RegistryFilter(orgCodes=(123,)))  # type: ignore[arg-type]

    def test_字面量两侧空白被裁剪(self) -> None:
        """与写侧 ``_codeList`` 同口径（strip）⇒ 两侧对同一码的写法一致。"""
        expr = buildRegistryFilterExpr(RegistryFilter(domainCodes=(" SUP ",)))

        assert expr == 'array_contains(domain_codes, "SUP")'

    def test_重复字面量被去重(self) -> None:
        expr = buildRegistryFilterExpr(RegistryFilter(domainCodes=("SUP", "SUP")))

        assert expr == 'array_contains(domain_codes, "SUP")'


class Test非法字面量在闸门里fail_closed:
    def test_闸门把非法字面量转成空结果(self, switchOn, monkeypatch) -> None:
        """表达式构造失败 ⇒ 空结果（``filter_invalid``），绝不回落成未过滤的结果。"""
        calls = _installV1(monkeypatch)
        exprs = _installV2(monkeypatch)

        result = gate.searchRegistryEmbeddings(
            [0.1] * 4, topK=5, registryFilter=RegistryFilter(entityTypes=("",))
        )

        assert calls == []
        assert exprs == []
        assert result.hits == []
        assert result.degradedReason == "filter_invalid"
