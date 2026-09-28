export interface MenuItem {
  code: string;
  labelKey: string;
  iconCode: string | null;
  sortOrder: number;
  permissionCode: string | null;
  roles: string[];
  path: string | null;
}

export interface MenuSection extends MenuItem {
  children: MenuItem[];
}

export interface MenuConfig {
  version: string;
  sections: MenuSection[];
}

/** 菜单管理页扁平行（含不可见项 / parent 信息），镜像后端 MenuRowRead。
 *
 * isSection：path 为空 → 一级类；否则叶子项（授权以叶子 code 为键）。
 */
export interface MenuRow {
  id: number;
  code: string;
  parentId: number | null;
  parentCode: string | null;
  labelKey: string;
  iconCode: string | null;
  sortOrder: number;
  path: string | null;
  permissionCode: string | null;
  visible: boolean;
  hasChildren: boolean;
}

export interface MenuCreatePayload {
  code: string;
  labelKey: string;
  iconCode?: string | null;
  sortOrder?: number;
  path?: string | null;
  parentCode?: string | null;
  permissionCode?: string | null;
  visible?: boolean;
}

export interface MenuUpdatePayload {
  labelKey?: string;
  iconCode?: string | null;
  sortOrder?: number;
  path?: string | null;
  parentCode?: string | null;
  permissionCode?: string | null;
  visible?: boolean;
}

/** 菜单管理页树形视图节点（feat-menu-tree），镜像后端 MenuConfigTreeNode。
 *
 * 与 MenuRow 的区别：嵌套 children + parentCode（不是 parentId）。
 * path === null 表示 section（一级类），UI 上挂其它节点。
 * 当前 seed 仅 2 层（section→item），但类型上 children 仍递归，支持任意层数。
 */
export interface MenuConfigTreeNode {
  code: string;
  labelKey: string;
  iconCode: string | null;
  sortOrder: number;
  path: string | null;
  permissionCode: string | null;
  visible: boolean;
  parentCode: string | null;
  children: MenuConfigTreeNode[];
}
