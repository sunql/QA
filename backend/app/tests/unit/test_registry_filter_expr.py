"""P3a D19：RAG 过滤表达式的构造（纯函数，不碰 Milvus）。

为什么这层值得单独钉死
----------------------
过滤表达式是**安全边界**：过滤写错 = 越权召回。Milvus 2.4.6 上实测到的三条
硬约束（2026-11-15，探针 `/tmp/probe_array.py`）都指向同一个后果 ——
表达式一旦回退到「看起来等价」的写法，过滤会**静默失效**：

- ARRAY 字段上 ``in`` / ``like`` **直接 query plan 解析失败**
  ⇒ 不能用 ``in [...]`` 或 ``like "SUP"`` 糊弄；
- ``not array_contains(...)`` **会命中空数组行**
  ⇒ 「排除某域」必须写成**显式枚举允许域**，否定式会让未归域的行漏进来；
- 空数组是合法值（``enable_dynamic_field=false`` 下缺字段写不进去）
  ⇒ 未归域必须显式表达，不能靠「不写这个字段」。

另外字面量转义也是安全面：域/组织码来自治理表（可被人工编辑），
一个未经转义的 ``"`` 就能把表达式拆开 ⇒ 注入面。故本文件钉死转义行为。

全部用 fake / 纯函数 —— 不连 Milvus（本地 19530 是生产 Milvus，禁跑真连接）。
"""

from __future__ import annotations

import pytest

from app.domain.registry_filter import RegistryFilter
from app.infrastructure.milvus_registry_v2 import buildRegistryFilterExpr

# ---------------------------------------------------------------------------
# 无过滤
# ---------------------------------------------------------------------------


def test_没有过滤器时不产生条件() -> None:
    assert buildRegistryFilterExpr(None) is None


def test_全空过滤器不产生条件() -> None:
    assert buildRegistryFilterExpr(RegistryFilter()) is None


def test_空元组维度不产生条件() -> None:
    """空元组 ≠ 「匹配任何值」—— 空集合不该变成一个恒真条件。"""
    assert buildRegistryFilterExpr(
        RegistryFilter(domainCodes=(), orgCodes=(), entityTypes=())
    ) is None


def test_声明了作用域但取值为空时不产生条件() -> None:
    """「声明了作用域但取值为空」⇒ 表达式层无法表达 ⇒ 返回 None。

    ⚠️ **返回 None 不等于「不过滤」**：闸门看到「有作用域 + 无表达式」
    必须判成 deny-all（空结果），而不是拿无过滤的答案冒充。
    这个分工由闸门单测钉死。

    D21R2 起「声明了作用域」由**过滤器的存在**表达（无标志位），
    故这里用一个空过滤器即可。
    """
    assert buildRegistryFilterExpr(RegistryFilter()) is None


# ---------------------------------------------------------------------------
# 单维度
# ---------------------------------------------------------------------------


def test_单域用array_contains() -> None:
    expr = buildRegistryFilterExpr(RegistryFilter(domainCodes=("SUP",)))
    assert expr == 'array_contains(domain_codes, "SUP")'


def test_多域用or连接() -> None:
    expr = buildRegistryFilterExpr(RegistryFilter(domainCodes=("SUP", "MAT")))
    assert expr == (
        'array_contains(domain_codes, "SUP") or array_contains(domain_codes, "MAT")'
    )


def test_多组织用or连接() -> None:
    expr = buildRegistryFilterExpr(RegistryFilter(orgCodes=("ORG_A", "ORG_B")))
    assert expr == (
        'array_contains(org_codes, "ORG_A") or array_contains(org_codes, "ORG_B")'
    )


def test_实体类型用标量等值() -> None:
    expr = buildRegistryFilterExpr(RegistryFilter(entityTypes=("SUPPLIER",)))
    assert expr == 'entity_type == "SUPPLIER"'


def test_多实体类型用or连接() -> None:
    expr = buildRegistryFilterExpr(RegistryFilter(entityTypes=("SUPPLIER", "MATERIAL")))
    assert expr == 'entity_type == "SUPPLIER" or entity_type == "MATERIAL"'


def test_发布版本用标量等值() -> None:
    expr = buildRegistryFilterExpr(RegistryFilter(releaseId=42))
    assert expr == "release_id == 42"


# ---------------------------------------------------------------------------
# 非法发布版本（哨兵冲突）
# ---------------------------------------------------------------------------


def test_release_id为0不是合法过滤条件() -> None:
    """``release_id == 0`` 是「无激活发布」的**哨兵值**，不是发布版本。

    放它进表达式 ⇒ 过滤条件退化成「召回所有没有激活发布的行」，
    而调用方以为自己限定了某个发布 ⇒ 口径完全错位 ⇒ 必须当非法输入拒绝。
    """
    with pytest.raises(ValueError):
        buildRegistryFilterExpr(RegistryFilter(releaseId=0))


def test_release_id为负数不是合法过滤条件() -> None:
    with pytest.raises(ValueError):
        buildRegistryFilterExpr(RegistryFilter(releaseId=-1))


def test_有效期用闭开区间且把零当长期有效() -> None:
    """valid_to == 0 = 长期有效（哨兵），不能被区间排除掉。"""
    expr = buildRegistryFilterExpr(RegistryFilter(validAt=1_700_000_000_000))
    assert expr == (
        "valid_from <= 1700000000000 and (valid_to == 0 or valid_to >= 1700000000000)"
    )


# ---------------------------------------------------------------------------
# 组合
# ---------------------------------------------------------------------------


def test_多维度用and连接() -> None:
    expr = buildRegistryFilterExpr(
        RegistryFilter(domainCodes=("SUP",), releaseId=7, validAt=1000)
    )
    assert expr == (
        'array_contains(domain_codes, "SUP")'
        " and release_id == 7"
        " and valid_from <= 1000 and (valid_to == 0 or valid_to >= 1000)"
    )


def test_表达式里绝不出现否定式array_contains() -> None:
    """否定式会命中空数组行（实测）⇒ 未归域的行会被当成「非该域」放进来。

    排除语义必须**显式枚举允许域**；本守卫防止后人图省事写回否定式。
    """
    expr = buildRegistryFilterExpr(
        RegistryFilter(
            domainCodes=("SUP",),
            orgCodes=("ORG_A",),
            entityTypes=("SUPPLIER",),
            releaseId=1,
            validAt=1,
        )
    )
    assert expr is not None
    assert "not " not in expr
    assert "array_contains" in expr


# ---------------------------------------------------------------------------
# 字面量转义（注入面）
# ---------------------------------------------------------------------------


def test_双引号被转义() -> None:
    expr = buildRegistryFilterExpr(RegistryFilter(domainCodes=('A"B',)))
    assert expr == 'array_contains(domain_codes, "A\\"B")'


def test_反斜杠被转义() -> None:
    expr = buildRegistryFilterExpr(RegistryFilter(orgCodes=("A\\B",)))
    assert expr == 'array_contains(org_codes, "A\\\\B")'


def test_实体类型里的双引号被转义() -> None:
    expr = buildRegistryFilterExpr(RegistryFilter(entityTypes=('X"Y',)))
    assert expr == 'entity_type == "X\\"Y"'


def test_非ascii码原样保留() -> None:
    """中文域码/组织码是合法业务值（实测 Milvus 接受中文字面量）。"""
    expr = buildRegistryFilterExpr(RegistryFilter(orgCodes=("采购部门",)))
    assert expr == 'array_contains(org_codes, "采购部门")'


def test_转义后不产生裸引号() -> None:
    """除成对定界引号外不得再出现裸双引号。"""
    expr = buildRegistryFilterExpr(
        RegistryFilter(domainCodes=('A"B', 'C"D'), entityTypes=('E"F',))
    )
    assert expr is not None
    # 去掉转义引号后，剩下的双引号数必须是偶数（每个字面量一对定界引号）
    assert expr.replace('\\"', "").count('"') % 2 == 0
