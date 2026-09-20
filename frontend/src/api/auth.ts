// 认证 API（feat-user-auth，2026-09-20）
//
// 对齐后端 app/api/v1/auth.py 5 个端点。所有响应经统一 httpClient 拦截器
// 解包 ApiResponse 信封，直接返回 data 字段。
//
// 401 拦截在 client.ts 的 _handleUnauthorized() —— authApi 不重复处理。
// logout() 在 token 已失效时仍可调用 —— 拦截器会清 store 但本次 logout
// 自己会抛 401；调用方应 try/catch 后清本地状态。

import { httpClient } from "./client";
import type {
  AdminResetPasswordRequest,
  AuthChangePasswordRequest,
  AuthLoginRequest,
  AuthLoginResponse,
  AuthMeRead,
  PasswordPolicyRead,
} from "../types/auth";

const PREFIX = "/auth";

export const authApi = {
  async login(payload: AuthLoginRequest): Promise<AuthLoginResponse> {
    const res = await httpClient.post<AuthLoginResponse>(`${PREFIX}/login`, payload);
    return res.data;
  },

  async me(): Promise<AuthMeRead> {
    const res = await httpClient.get<AuthMeRead>(`${PREFIX}/me`);
    return res.data;
  },

  async logout(): Promise<void> {
    await httpClient.post(`${PREFIX}/logout`);
  },

  async changeOwnPassword(payload: AuthChangePasswordRequest): Promise<void> {
    await httpClient.put(`${PREFIX}/me/password`, payload);
  },

  async passwordPolicy(): Promise<PasswordPolicyRead> {
    const res = await httpClient.get<PasswordPolicyRead>(`${PREFIX}/password-policy`);
    return res.data;
  },

  async adminResetPassword(
    userId: number,
    payload: AdminResetPasswordRequest,
  ): Promise<void> {
    await httpClient.put(`/users/${userId}/password`, payload);
  },
};
