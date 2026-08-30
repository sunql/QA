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

        失败 → PermissionDeniedError（HTTP 403），**消息不暴露** owner /
        entity_code / user.departments 等内部状态（防枚举侧信道）。

        entity_owner 为空时，仅 admin 可改（避免「无主 KPI 任意改」的隐患）。
        """
        if ADMIN_ROLE in user.roles:
            return

        owner = (entity_owner or "").strip()
        if owner and owner in user.departments:
            return

        # 通用 403 消息：仅说明权限策略，不暴露 owner / 当前用户部门等内部状态。
        # 实测上下文（哪个 entity / 哪个 user）已写入服务端日志，便于运维审计。
        logger.info(
            "ACL denied: label=%s code=%s actor=%s owner_set=%s",
            entity_label,
            entity_code,
            user.userId,
            bool(owner),
        )
        raise PermissionDeniedError(
            "无权修改该资源：仅 owner 部门成员或 admin 角色可操作"
        )