"""AclService 单元测试（Phase 4.5 governance hardening）。

纯函数逻辑（无 DB / 无 IO），不依赖 fixtures。
"""

from __future__ import annotations

import pytest

from app.dependencies import CurrentUser
from app.domain.exceptions import PermissionDeniedError
from app.services.acl_service import ADMIN_ROLE, AclService


def _user(roles=("user",), departments=()):
    return CurrentUser(userId="u1", roles=roles, departments=departments)


class TestAclService:
    def setup_method(self) -> None:
        self._svc = AclService()

    def test_admin_role_passes_any_owner(self) -> None:
        """admin 角色可改任意 owner 的 KPI。"""
        self._svc.assertCanModify(_user(roles=(ADMIN_ROLE, "user")), "采购部", "KPI", "X")
        self._svc.assertCanModify(_user(roles=(ADMIN_ROLE,)), "", "KPI", "X")
        self._svc.assertCanModify(_user(roles=(ADMIN_ROLE,)), "不存在的部门", "KPI", "X")

    def test_owner_department_match_passes(self) -> None:
        """user.departments 含 owner → 通过。"""
        self._svc.assertCanModify(_user(departments=("采购部",)), "采购部", "KPI", "X")

    def test_owner_department_match_passes_when_user_has_multiple(self) -> None:
        """多部门成员：任一部门匹配即可。"""
        self._svc.assertCanModify(
            _user(departments=("采购部", "财务部")), "财务部", "KPI", "X"
        )

    def test_other_department_denied(self) -> None:
        """部门不匹配 + 无 admin 角色 → 拒绝。"""
        with pytest.raises(PermissionDeniedError) as excInfo:
            self._svc.assertCanModify(
                _user(departments=("财务部",)), "采购部", "KPI", "KPI_SUPPLIER_OTD"
            )
        assert "KPI_SUPPLIER_OTD" in str(excInfo.value)

    def test_empty_owner_denies_non_admin(self) -> None:
        """owner 为空 + 无 admin → 拒绝（避免「无主 KPI 任意改」）。"""
        with pytest.raises(PermissionDeniedError):
            self._svc.assertCanModify(_user(departments=("采购部",)), "", "KPI", "X")

    def test_empty_owner_admin_passes(self) -> None:
        """owner 为空 + admin → 通过（admin 是兜底）。"""
        self._svc.assertCanModify(_user(roles=(ADMIN_ROLE,)), "", "KPI", "X")

    def test_no_departments_no_admin_denied(self) -> None:
        """user.departments 为空 + 无 admin + owner 非空 → 拒绝。"""
        with pytest.raises(PermissionDeniedError):
            self._svc.assertCanModify(_user(), "采购部", "KPI", "X")

    def test_message_contains_owner_and_user_departments(self) -> None:
        """错误消息含 owner 与 user.departments（便于调试/合规审计）。"""
        with pytest.raises(PermissionDeniedError) as excInfo:
            self._svc.assertCanModify(
                _user(departments=("财务部",)), "采购部", "KPI", "KPI_SUPPLIER_OTD"
            )
        msg = str(excInfo.value)
        assert "采购部" in msg
        assert "财务部" in msg

    def test_whitespace_owner_stripped(self) -> None:
        """owner 前后空白应被剥离后再比较。"""
        # owner " 采购部 " 应与 "采购部" 等价
        self._svc.assertCanModify(_user(departments=("采购部",)), " 采购部 ", "KPI", "X")

    def test_acl_service_is_stateless(self) -> None:
        """AclService 无内部状态；同一实例多次调用互不影响。"""
        svc = AclService()
        svc.assertCanModify(_user(roles=(ADMIN_ROLE,)), "采购部", "KPI", "X")
        with pytest.raises(PermissionDeniedError):
            svc.assertCanModify(_user(), "采购部", "KPI", "X")
