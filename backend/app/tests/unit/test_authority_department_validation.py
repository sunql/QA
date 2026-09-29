"""authority_department 字段校验的纯逻辑测试（v3.1 任务 M5 / 蓝图 §4.13）。

决策 D2=双列：authority_level L0-L5 + 新增 authority_department（11 部门枚举）。
本文件盯 DTO（WikiPageCreate / WikiPageUpdate / WikiPageRead）+ ORM 元组
（KNOWLEDGE_AUTHORITY_DEPARTMENTS）的校验闭环：

- **11 枚举值**全部接受（按业务约定排序 9 个常规 + 2 个软约束占位）
- **None** 接受（旧数据/治理未推动时合法，与「漏传」语义不同）
- **非法值** 拒绝（Pydantic ValidationError → 422）
- **超长值** 拒绝（max_length=30，超出会先于白名单兜底）
- ORM 元组与 DTO 引用保持同步（避免某天改一边忘另一边）
- service 层 ``_assertKnowledgeAuthorityDepartment`` 独立可调用：
  None 通过；非法值抛 HTTPException 400（与 _assertKnowledgeAuthorityLevel
  同模式，DTO 422 + service 400 双层防御）。

不触 DB / 不触网络。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.wiki_models import (
    KNOWLEDGE_AUTHORITY_DEPARTMENTS,
    KNOWLEDGE_AUTHORITY_LEVELS,
)
from app.domain.wiki_schemas import WikiPageCreate, WikiPageRead, WikiPageUpdate
from app.services.wiki_page_service import (
    _assertKnowledgeAuthorityDepartment,
    _assertKnowledgeAuthorityLevel,
)

# 11 部门枚举完整集（与 KNOWLEDGE_AUTHORITY_DEPARTMENTS 一一对应）。单独列出
# 便于：① 失败时定位是 ORM 元组漏了还是 schema 漏了；② 软约束占位（后 2 个）
# 与常规组织（前 9 个）一眼可分。
_EXPECTED_11 = (
    "SALES_MGMT",
    "FINANCE",
    "SCM",
    "QA",
    "HR",
    "IT",
    "OPS",
    "EXEC",
    "LEGAL",
    "INDUSTRY_STANDARD",
    "CROSS_DOMAIN",
)


def test_orm_tuple_has_eleven_departments_in_expected_order() -> None:
    """钉死 ORM 元组：长度 11、排序与业务约定一致。"""
    assert len(KNOWLEDGE_AUTHORITY_DEPARTMENTS) == 11
    assert KNOWLEDGE_AUTHORITY_DEPARTMENTS == _EXPECTED_11


def test_orm_tuple_disjoint_from_authority_levels() -> None:
    """authority_department 与 authority_level 是不同轴，元组互不重叠。"""
    overlap = set(KNOWLEDGE_AUTHORITY_DEPARTMENTS) & set(KNOWLEDGE_AUTHORITY_LEVELS)
    assert overlap == set(), f"轴重叠（双轴并存语义被破坏）：{overlap}"


@pytest.mark.parametrize("department", _EXPECTED_11)
def test_wiki_page_create_accepts_all_eleven_departments(department: str) -> None:
    """11 枚举值在 Create 都能过（保证 ORM 元组与 DTO 引用一致）。"""
    dto = WikiPageCreate(
        title="供应商准入规则",
        content="注册资本 >= 1000 万",
        authority_department=department,
    )
    assert dto.authority_department == department


def test_wiki_page_create_accepts_none_department() -> None:
    """None 合法：旧数据/治理未推动场景必走 NULL。"""
    dto = WikiPageCreate(
        title="x",
        content="y",
        authority_department=None,
    )
    assert dto.authority_department is None


def test_wiki_page_create_rejects_invalid_department() -> None:
    """非法值 → Pydantic ValidationError（DTO 422 路径）。"""
    with pytest.raises(ValidationError) as exc:
        WikiPageCreate(
            title="x",
            content="y",
            authority_department="NOT_A_REAL_DEPARTMENT",
        )
    assert "authority_department" in str(exc.value)


def test_wiki_page_create_rejects_lowercase_department() -> None:
    """小写变体视为非法（与 authority_level L0-L5 同：枚举字面精确比对）。

    否则前后端大小写规则漂移会让「同一份文档跨端渲染出两个不同 badge」。
    """
    with pytest.raises(ValidationError):
        WikiPageCreate(
            title="x",
            content="y",
            authority_department="finance",  # 应为 FINANCE
        )


def test_wiki_page_create_rejects_overlong_department() -> None:
    """超长字符串先被 max_length=30 拦下（与 ORM 列对齐）。"""
    with pytest.raises(ValidationError):
        WikiPageCreate(
            title="x",
            content="y",
            authority_department="A" * 31,
        )


def test_wiki_page_update_accepts_each_department() -> None:
    """PATCH 同款：11 枚举 + None 全部接受。"""
    for dept in _EXPECTED_11:
        dto = WikiPageUpdate(authority_department=dept)
        assert dto.authority_department == dept


def test_wiki_page_update_unset_default_skips_validation() -> None:
    """PATCH 不传 authority_department 字段 → UNSET 哨兵 → 不参与校验。

    这是 Update 与 Create 的关键差异：Create 没 UNSET 概念，「未提供」就是默认
    None；Update 必须区分「没传」与「传了 null」（前者跳过，后者置空）。
    """
    dto = WikiPageUpdate(title="new title")
    # UNSET 哨兵 + 不应抛错
    assert dto.model_fields_set == {"title"}


def test_wiki_page_update_explicit_none_clears_memo() -> None:
    """PATCH 显式传 null = 置空部门字段（与 dimension 同语义）。"""
    dto = WikiPageUpdate(authority_department=None)
    assert dto.authority_department is None


def test_wiki_page_update_rejects_invalid_department() -> None:
    """PATCH 非法值仍然 422（与 Create 同款校验）。"""
    with pytest.raises(ValidationError):
        WikiPageUpdate(authority_department="BOGUS")


def test_wiki_page_read_passes_department_through() -> None:
    """Read 模型：合法值透传，None 透传，非法字符过滤（不抛，因读模型不做严格校验）。

    读模型不该在序列化时抛错 —— 旧数据迁移时可能已存在脏值，读路径要稳。
    """
    dto = WikiPageRead.model_validate({
        "id": 1,
        "page_id": "PAGE-X",
        "title": "x",
        "content": "y",
        "structure_stage": "MARKDOWN",
        "status": "DRAFT",
        "version": "v1.0",
        "authority_department": "FINANCE",
    })
    assert dto.authority_department == "FINANCE"

    dtoNone = WikiPageRead.model_validate({
        "id": 1,
        "page_id": "PAGE-X",
        "title": "x",
        "content": "y",
        "structure_stage": "MARKDOWN",
        "status": "DRAFT",
        "version": "v1.0",
        "authority_department": None,
    })
    assert dtoNone.authority_department is None


def test_service_assert_knowledge_authority_department_passes_none() -> None:
    """service 兜底：None 通过（治理流程可推动后才会填）。"""
    _assertKnowledgeAuthorityDepartment(None)  # 不抛


def test_service_assert_knowledge_authority_department_passes_each_value() -> None:
    """service 兜底：11 枚举全部通过。"""
    for dept in _EXPECTED_11:
        _assertKnowledgeAuthorityDepartment(dept)  # 不抛


def test_service_assert_knowledge_authority_department_rejects_invalid() -> None:
    """service 兜底：非法值抛 HTTPException 400（与 _assertKnowledgeAuthorityLevel 同形）。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _assertKnowledgeAuthorityDepartment("FAKE_DEPT")
    assert exc.value.status_code == 400
    assert "权威归属部门非法" in str(exc.value.detail)


def test_service_assert_knowledge_authority_level_untouched() -> None:
    """回归：authority_level 兜底与新增 department 兜底互不干扰。

    硬约束：M5 不能改 ``_assertKnowledgeAuthorityLevel`` 任何代码路径。
    """
    from fastapi import HTTPException

    # 合法值：仍然通过
    _assertKnowledgeAuthorityLevel("L3")
    # 非法值：仍然 400（不是 422，因为 service 是 400 兜底层）
    with pytest.raises(HTTPException) as exc:
        _assertKnowledgeAuthorityLevel("L9")
    assert exc.value.status_code == 400