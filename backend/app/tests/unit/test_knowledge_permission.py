"""P4 统一权限源（`knowledge/permission.py`）的**纯推导**部分。

为什么要有这一层
----------------
仓内至今**没有「用户 → 可见域 / 组织」的策略源**（P3a 的模块文档明写：
「现有 ACL 只有 admin 级的 `assertCanModify`」）⇒ P3a 把维度做成了
「调用方显式传入」，**策略接线被明确留给后续**。P4 就是那个「后续」。

推导口径
--------
1. `isAdmin` ⇒ 可见**全部 enabled 的域**；否则可见域 = 域的主组织 code ∈ 用户部门。
2. `primary_org_id IS NULL` 的域对**非 admin 一律不可见**（刻意的 fail-closed，
   遗留 L-P4-1）。
3. `entityTypes` = 可见域下的业务对象 code。
4. **请求里的 scope 只能收窄**（交集），不能放宽。

本文件只测**纯函数**（DB 读取那一层由集成测试覆盖）。
"""

from __future__ import annotations

from app.services.knowledge.permission import (
    deriveEntityTypes,
    deriveVisibleDomains,
    narrowDomainsByOrgs,
    narrowEntityTypes,
    narrowSecurityLevel,
    resolveAllowedOrgCodes,
)

#: (域 code, 主组织 code)；主组织为 None ⇒ `primary_org_id IS NULL`。
_DOMAIN_ROWS: list[tuple[str, str | None]] = [
    ("PROCUREMENT", "PROCUREMENT_ORG"),
    ("QUALITY", "QUALITY_ORG"),
    ("LOGISTICS", None),
]


def test_adminSeesEveryEnabledDomain() -> None:
    visible = deriveVisibleDomains(
        _DOMAIN_ROWS, departments=(), isAdmin=True
    )

    assert visible == ("LOGISTICS", "PROCUREMENT", "QUALITY")


def test_nonAdminSeesOnlyDomainsOfOwnDepartments() -> None:
    visible = deriveVisibleDomains(
        _DOMAIN_ROWS, departments=("PROCUREMENT_ORG",), isAdmin=False
    )

    assert visible == ("PROCUREMENT",)


def test_nonAdminCannotSeeDomainWithoutPrimaryOrg() -> None:
    """`primary_org_id IS NULL` 的域对非 admin 不可见（fail-closed）。"""
    visible = deriveVisibleDomains(
        _DOMAIN_ROWS, departments=("LOGISTICS_ORG",), isAdmin=False
    )

    assert "LOGISTICS" not in visible
    assert visible == ()


def test_nonAdminWithoutDepartmentsSeesNothing() -> None:
    visible = deriveVisibleDomains(_DOMAIN_ROWS, departments=(), isAdmin=False)

    assert visible == ()


def test_entityTypesComeFromVisibleDomains() -> None:
    entityTypes = deriveEntityTypes(
        [
            ("SUPPLIER", "PROCUREMENT"),
            ("PURCHASE_ORDER", "PROCUREMENT"),
            ("INSPECTION_RECORD", "QUALITY"),
        ],
        visibleDomains=("PROCUREMENT",),
    )

    assert entityTypes == ("PURCHASE_ORDER", "SUPPLIER")


def test_entityTypesAreEmptyForEmptyVisibleDomains() -> None:
    entityTypes = deriveEntityTypes(
        [("SUPPLIER", "PROCUREMENT")], visibleDomains=()
    )

    assert entityTypes == ()


def test_entityTypesIncludeBusinessObjectsAttachedToVisibleDomain() -> None:
    """**附域**的业务对象也算该域可见（与域切片的归属口径一致）。

    权限侧若只看主域，两端口径会漂移 ⇒ 切片里有、权限侧看不见
    ⇒ 请求侧收窄会误删本域条目。
    """
    entityTypes = deriveEntityTypes(
        [
            ("SUPPLIER", "PROCUREMENT"),
            ("INSPECTION_RECORD", "QUALITY"),
            ("INSPECTION_RECORD", "PROCUREMENT"),
        ],
        visibleDomains=("PROCUREMENT",),
    )

    assert entityTypes == ("INSPECTION_RECORD", "SUPPLIER")


def test_requestOrgCodesNarrowDomainSet() -> None:
    narrowed = narrowDomainsByOrgs(
        _DOMAIN_ROWS,
        visibleDomains=("PROCUREMENT", "QUALITY"),
        allowedOrgCodes=("QUALITY_ORG",),
    )

    assert narrowed == ("QUALITY",)


def test_requestOrgCodesDropDomainWithoutPrimaryOrg() -> None:
    """主组织为空的域无法按组织命中 ⇒ 被收窄掉。"""
    narrowed = narrowDomainsByOrgs(
        _DOMAIN_ROWS,
        visibleDomains=("LOGISTICS", "PROCUREMENT"),
        allowedOrgCodes=("PROCUREMENT_ORG",),
    )

    assert narrowed == ("PROCUREMENT",)


def test_noOrgNarrowingKeepsAllVisibleDomains() -> None:
    """`None` = 不按组织收窄（admin 未指定组织时）。"""
    narrowed = narrowDomainsByOrgs(
        _DOMAIN_ROWS,
        visibleDomains=("LOGISTICS", "PROCUREMENT"),
        allowedOrgCodes=None,
    )

    assert narrowed == ("LOGISTICS", "PROCUREMENT")


def test_allowedOrgCodesAreIntersectionForNonAdmin() -> None:
    allowed = resolveAllowedOrgCodes(
        ("PROCUREMENT_ORG",),
        ("PROCUREMENT_ORG", "QUALITY_ORG"),
        isAdmin=False,
    )

    assert allowed == ("PROCUREMENT_ORG",)


def test_allowedOrgCodesAreUsedAsIsForAdmin() -> None:
    """admin 不受组织归属限制 ⇒ 请求的组织码直接生效。"""
    allowed = resolveAllowedOrgCodes(
        (),
        ("QUALITY_ORG",),
        isAdmin=True,
    )

    assert allowed == ("QUALITY_ORG",)


def test_allowedOrgCodesAreNoneForAdminWithoutRequestScope() -> None:
    """admin 未指定组织 ⇒ 不按组织收窄（None 而非空 tuple）。"""
    allowed = resolveAllowedOrgCodes((), (), isAdmin=True)

    assert allowed is None


def test_allowedOrgCodesFallBackToDepartmentsForNonAdmin() -> None:
    allowed = resolveAllowedOrgCodes(
        ("PROCUREMENT_ORG",), (), isAdmin=False
    )

    assert allowed == ("PROCUREMENT_ORG",)


def test_requestCanOnlyNarrowEntityTypes() -> None:
    assert narrowEntityTypes(("SUPPLIER", "PURCHASE_ORDER"), ("SUPPLIER",)) == (
        "SUPPLIER",
    )


def test_emptyRequestEntityTypesKeepAll() -> None:
    assert narrowEntityTypes(("SUPPLIER",), ()) == ("SUPPLIER",)


def test_requestCanOnlyNarrowSecurityLevel() -> None:
    assert narrowSecurityLevel("L1", "L3") == "L1"
    assert narrowSecurityLevel("L3", "L1") == "L1"
    assert narrowSecurityLevel("L2", "L2") == "L2"


def test_unspecifiedSecurityLevelKeepsTheAuthoritativeValue() -> None:
    """`None`（没指定）⇒ 保留权威值（admin 的 L3 不该被默认值压成 L1）。"""
    assert narrowSecurityLevel("L3", None) == "L3"
    assert narrowSecurityLevel("L1", None) == "L1"
