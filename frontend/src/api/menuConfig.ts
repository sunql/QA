import { httpClient } from "./client";
import type {
  MenuConfig,
  MenuConfigTreeNode,
  MenuCreatePayload,
  MenuRow,
  MenuUpdatePayload,
} from "../types/menuConfig";

const API_BASE = "/api/v1";

export async function fetchMenuConfig(): Promise<MenuConfig> {
  const resp = await fetch(`${API_BASE}/menu-config`, {
    headers: { Accept: "application/json" },
    credentials: "include",
  });
  if (!resp.ok) {
    throw new Error(`fetchMenuConfig failed: ${resp.status} ${resp.statusText}`);
  }
  return (await resp.json()) as MenuConfig;
}

/** 管理面常量：菜单管理路由共享 PREFIX（router 无内部前缀，挂 /api/v1/menu-config）。 */
const MENU_ADMIN_PREFIX = "/menu-config";

/** 菜单管理页：全量扁平行（含不可见项 / parentCode / hasChildren）。 */
export async function listMenuRows(): Promise<MenuRow[]> {
  const res = await httpClient.get<MenuRow[]>(`${MENU_ADMIN_PREFIX}/admin`);
  return res.data;
}

/** 菜单管理页：嵌套树形结构（feat-menu-tree），供 antd Tree + 拖拽改父级。 */
export async function listMenuTree(): Promise<MenuConfigTreeNode[]> {
  const res = await httpClient.get<MenuConfigTreeNode[]>(
    `${MENU_ADMIN_PREFIX}/tree`,
  );
  return res.data;
}

/** 动态新增菜单节点（path 为空 → section，否则挂 parentCode 的叶子项）。 */
export async function createMenu(
  payload: MenuCreatePayload,
): Promise<MenuRow> {
  const res = await httpClient.post<MenuRow>(MENU_ADMIN_PREFIX, payload);
  return res.data;
}

/** 编辑菜单节点（PATCH 语义）。 */
export async function updateMenu(
  code: string,
  payload: MenuUpdatePayload,
): Promise<MenuRow> {
  const res = await httpClient.put<MenuRow>(
    `${MENU_ADMIN_PREFIX}/${encodeURIComponent(code)}`,
    payload,
  );
  return res.data;
}

/** 删除菜单节点（section 含子项 → 409）。 */
export async function deleteMenu(code: string): Promise<void> {
  await httpClient.delete(
    `${MENU_ADMIN_PREFIX}/${encodeURIComponent(code)}`,
  );
}

