// 裸 axios 调用的头注入（feat-user-auth，2026-09-20）
//
// 适用范围：上传 / SSE / FormData / 二进制流等不走 httpClient（axios.create
// 实例）的路径。这些路径需要手动加 X-Tenant-Id + Authorization。
//
// 用法：
//   import { authHeaders } from "./authHeaders";
//   await axios.postForm(url, form, { headers: authHeaders() });

import { DEFAULT_TENANT_ID } from "../config";
import { useAuthStore } from "../stores/authStore";

export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = useAuthStore.getState().token;
  const headers: Record<string, string> = {
    "X-Tenant-Id": DEFAULT_TENANT_ID,
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  if (extra) {
    Object.assign(headers, extra);
  }
  return headers;
}
