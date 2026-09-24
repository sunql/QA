import { httpClient } from "./client";
import type {
  MenuConfig,
  MenuConfigTreeNode,
  MenuCreatePayload,
  MenuRow,
  MenuUpdatePayload,
} from "../types/menuConfig";

// 用相对路径（httpClient.baseURL 已含 /api/v1）；不要拼 `${API_BASE}/menu-config`，
// 否则 axios 会拼出 `/api/v1/api/v1/menu-config` → 404 → AppLayout 退回扁平 FALLBACK_NAV。
export async function fetchMenuConfig(): Promise<MenuConfig> {
  // 用 httpClient 而非裸 fetch：保证带上 X-Tenant-Id / X-User-Id 默认头。
  // 后端 AUTH_STUB_ENABLED=1（开发模式）依赖这两个头识别 actor，
  // 裸 fetch 漏头会得到 403 → AppLayout 退回 FALLBACK_NAV（扁平 22 项）。
  // 响应拦截器已解包 ApiResponse 信封，res.data 即 MenuConfig。
  const res = await httpClient.get<MenuConfig>("/menu-config");
  return res.data;
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

