"""Owner-based ACL 服务（Phase 4.5 governance hardening，遗留 #68）。

语义：用户能否修改某个 entity = (用户部门 ⊇ entity.owner) OR (user.roles ⊇ {'admin'})。

Phase 4.5 仅应用于 KpiCatalogService（kpi_catalog.owner 字段已存在）。
后续 Phase 可扩展到其他有 owner 字段的实体（ontology_class.object_owner /
entity_mapping / data_quality_rule ...）。

stub 鉴权说明：
当前 `getCurrentUser` 从 X-User-Id / X-User-Departments 头解析用户，
真实生产应由 JWT/IdP 解析并填充 departments。stub 模式可保证接口稳定，
待真实鉴权接入时 ACL 规则无需改动。
"""

from __future__ import annotations

import logging

from app.dependencies import CurrentUser
from app.domain.exceptions import PermissionDeniedError

logger = logging.getLogger(__name__)

ADMIN_ROLE = "admin"


class AclService:
    """权限断言；不通过则抛 PermissionDeniedError（→ HTTP 403）。"""

    def assertCanModify(
        self,
        user: CurrentUser,
        entity_owner: str | None,
        entity_label: str = "KPI",
        entity_code: str = "",
    ) -> None:
        """断言当前用户可修改 entity。

        通过条件（任一）：
            1. user.roles 含 'admin'
            2. entity_owner 非空 且 entity_owner ∈ user.departments

        失败 → PermissionDeniedError(message=f"无权修改 {entity_label}「{entity_code}」...")。

        entity_owner 为空时，仅 admin 可改（避免「无主 KPI 任意改」的隐患）。
        """
        if ADMIN_ROLE in user.roles:
            return

        owner = (entity_owner or "").strip()
        if owner and owner in user.departments:
            return

        raise PermissionDeniedError(
            f"无权修改 {entity_label}「{entity_code}」"
            f"（owner={owner or '∅'}，"
            f"当前用户部门={','.join(user.departments) or '∅'}）；"
            f"仅 owner 部门或 admin 角色可改"
        )