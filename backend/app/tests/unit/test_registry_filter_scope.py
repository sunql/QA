"""P3a 复核：过滤器的**作用域声明**语义（不可变值对象）。

为什么单独钉死
--------------
``hasScope()`` 决定闸门的**降级口径**：有作用域 ⇒ v2 不可用时必须 fail-closed
（空结果），无作用域才允许回落 v1（回落 = 把越权行放进来 = 泄漏）。

「有作用域」有两种表达：

1. 给了任一维度的**取值**（域码 / 组织码 / 实体码 / 发布版本 / 有效期）；
2. 调用方**声明了作用域但取值为空**（``isScopeBound=True``）——
   例如「我这一轮有权看的域是空集」⇒ 语义是**什么都不许看到**（deny-all），
   不是「没有作用域、随便看」。

第 2 种若无法表达，「空作用域」就会被当成「无作用域」⇒
恰好在「本轮什么都看不到」的场景下变成**全量可见** ⇒ 越权。

全部纯对象 —— 不连 Milvus、不连 PG。
"""

from __future__ import annotations

import dataclasses

import pytest

from app.domain.registry_filter import RegistryFilter


class Test作用域判定:
    def test_未声明作用域且各维度取值为空时过滤器认为无作用域(self) -> None:
        assert RegistryFilter().hasScope() is False

    def test_声明了作用域但各维度取值为空时过滤器认为有作用域(self) -> None:
        """「声明了作用域但取值为空」= deny-all 的语义，必须与「无作用域」区分开。"""
        assert RegistryFilter(isScopeBound=True).hasScope() is True

    def test_给了域码时过滤器认为有作用域(self) -> None:
        assert RegistryFilter(domainCodes=("SUP",)).hasScope() is True

    def test_给了组织码时过滤器认为有作用域(self) -> None:
        assert RegistryFilter(orgCodes=("ORG_A",)).hasScope() is True

    def test_给了实体码时过滤器认为有作用域(self) -> None:
        assert RegistryFilter(entityTypes=("SUPPLIER",)).hasScope() is True

    def test_给了发布版本时过滤器认为有作用域(self) -> None:
        assert RegistryFilter(releaseId=1).hasScope() is True

    def test_给了有效期时过滤器认为有作用域(self) -> None:
        assert RegistryFilter(validAt=1_700_000_000_000).hasScope() is True


class Test不可变性:
    def test_过滤器不可被改写(self) -> None:
        """过滤器是闸门的输入契约 —— 被就地改写会让「算好的作用域」失效。"""
        registryFilter = RegistryFilter(domainCodes=("SUP",))
        with pytest.raises(dataclasses.FrozenInstanceError):
            registryFilter.domainCodes = ("MAT",)  # type: ignore[misc]
