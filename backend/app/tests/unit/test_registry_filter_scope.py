"""P3a 复核：过滤器的**作用域语义**（不可变值对象）。

口径（D21R2 起）
---------------
**过滤器的「存在」本身就是作用域声明**：

- ``registryFilter=None`` ⇒ 本轮**无作用域**（不收窄）；
- 任何 ``RegistryFilter`` 实例 ⇒ **已声明作用域**；各维度取值为空 ⇒ deny-all。

为什么不用「标志位」表达
------------------------
上一轮用 ``isScopeBound=True`` 表达「声明了作用域但取值为空」，
但**默认那一种（不写标志位）仍是不安全的** —— ``RegistryFilter(domainCodes=())``
会被当成「无作用域」⇒ 恰好在本该最严格的一轮变成全量可见。
⇒ 标志位被下线（见 ``test_registry_filter_hardening.py``）：不写就不会错。

全部纯对象 —— 不连 Milvus、不连 PG。
"""

from __future__ import annotations

import dataclasses

import pytest

from app.domain.registry_filter import RegistryFilter


class Test过滤器契约:
    def test_不再有作用域标志位(self) -> None:
        """``isScopeBound`` 已下线 ⇒ 过滤器的字段集合就是「各维度的取值」。"""
        fieldNames = {f.name for f in dataclasses.fields(RegistryFilter)}

        assert fieldNames == {
            "domainCodes",
            "orgCodes",
            "entityTypes",
            "releaseId",
            "validAt",
        }

    def test_各维度默认值均为空(self) -> None:
        registryFilter = RegistryFilter()

        assert registryFilter.domainCodes == ()
        assert registryFilter.orgCodes == ()
        assert registryFilter.entityTypes == ()
        assert registryFilter.releaseId is None
        assert registryFilter.validAt is None

    def test_给了域码时取值得以保留(self) -> None:
        assert RegistryFilter(domainCodes=("SUP",)).domainCodes == ("SUP",)

    def test_给了组织码时取值得以保留(self) -> None:
        assert RegistryFilter(orgCodes=("ORG_A",)).orgCodes == ("ORG_A",)

    def test_给了实体码时取值得以保留(self) -> None:
        assert RegistryFilter(entityTypes=("SUPPLIER",)).entityTypes == ("SUPPLIER",)

    def test_给了发布版本时取值得以保留(self) -> None:
        assert RegistryFilter(releaseId=1).releaseId == 1

    def test_给了有效期时取值得以保留(self) -> None:
        assert RegistryFilter(validAt=1_700_000_000_000).validAt == 1_700_000_000_000


class Test不可变性:
    def test_过滤器不可被改写(self) -> None:
        """过滤器是闸门的输入契约 —— 被就地改写会让「算好的作用域」失效。"""
        registryFilter = RegistryFilter(domainCodes=("SUP",))
        with pytest.raises(dataclasses.FrozenInstanceError):
            registryFilter.domainCodes = ("MAT",)  # type: ignore[misc]
