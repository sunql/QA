/** RBAC 领域类型（feat-rbac-identity，镜像 backend/app/schemas/rbac.py 驼峰契约）。

 * 约定：CRUD payload / 视图字段与后端 CamelModel 的 JSON alias 完全一致
 * （snake_case 后端字段 → camelCase 网络键）。
 */

export interface UserRow {
    id: number;
    username: string;
    displayName: string;
    email: string | null;
    enabled: boolean;
    roleIds: number[];
    roleCodes: string[];
    organizationIds: number[];
    organizationCodes: string[];
    createdTime: string;
    updatedTime: string | null;
}

export interface UserCreatePayload {
    username: string;
    displayName: string;
    email?: string | null;
    enabled?: boolean;
}

export interface UserUpdatePayload {
    displayName?: string;
    email?: string | null;
    enabled?: boolean;
}

export interface RoleIdsUpdatePayload {
    roleIds: number[];
}

export interface OrganizationIdsUpdatePayload {
    organizationIds: number[];
}

export interface MenuCodesUpdatePayload {
    menuCodes: string[];
}

export interface RoleRow {
    id: number;
    code: string;
    name: string;
    description: string | null;
    isBuiltin: boolean;
    createdTime: string;
    updatedTime: string | null;
}

export interface RoleCreatePayload {
    code: string;
    name: string;
    description?: string | null;
}

export interface RoleUpdatePayload {
    name?: string;
    description?: string | null;
}

export interface OrganizationRow {
    id: number;
    code: string;
    name: string;
    parentId: number | null;
    description: string | null;
    sortOrder: number;
    createdTime: string;
    updatedTime: string | null;
}

export interface OrganizationTreeNode {
    id: number;
    code: string;
    name: string;
    sortOrder: number;
    description: string | null;
    children: OrganizationTreeNode[];
}

export interface OrganizationCreatePayload {
    code: string;
    name: string;
    parentId?: number | null;
    description?: string | null;
    sortOrder?: number;
}

export interface OrganizationUpdatePayload {
    name?: string;
    description?: string | null;
    parentId?: number | null;
    sortOrder?: number;
}

/** 授权来源拆分中的一条（角色 / 组织），供需求 #4 用户权限拆解视图展示。 */
export interface GrantSource {
    subjectId: number;
    code: string;
    name: string;
    menuCodes: string[];
}

/** 用户有效权限视图（需求 #4）：三来源合集 + 逐来源拆分。 */
export interface UserEffectivePermissions {
    userId: number;
    isSuperuser: boolean;
    roleCodes: string[];
    organizationCodes: string[];
    menuCodes: string[];
    directGrants: string[];
    roleGrants: GrantSource[];
    organizationGrants: GrantSource[];
}

/** 角色 / 组织维度直接授权视图（需求 #4）。 */
export interface SubjectPermissions {
    subjectId: number;
    menuCodes: string[];
}
