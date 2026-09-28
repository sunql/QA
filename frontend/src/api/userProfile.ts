/** 当前用户身份 API（个人中心页，2026-09-16）
 *
 * 后端 GET /api/v1/users/me（非 admin-only）：
 * - X-User-Id 命中 DB 用户 → displayName/email 取 users 行，角色/组织以 DB 为准；
 * - 桩回退 → dbUserId=null，displayName 回退 userId。
 */

import { httpClient } from "./client";

export interface CurrentUserMe {
  userId: string;
  displayName: string;
  email: string | null;
  roleCodes: string[];
  departmentCodes: string[];
  dbUserId: number | null;
}

export async function fetchCurrentUserMe(): Promise<CurrentUserMe> {
  const res = await httpClient.get<CurrentUserMe>("/users/me");
  return res.data;
}
