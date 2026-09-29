import { apiClient } from "./client";
import type {
  AuthLoginRequest,
  AuthLoginResponse,
  AuthMeRead,
  AuthChangePasswordRequest,
  PasswordPolicyRead,
} from "../types/auth";

export const authApi = {
  login: async (payload: AuthLoginRequest): Promise<AuthLoginResponse> => {
    const { data } = await apiClient.post<AuthLoginResponse>("/auth/login", payload);
    return data;
  },
  logout: async (): Promise<void> => {
    await apiClient.post("/auth/logout");
  },
  getMe: async (): Promise<AuthMeRead> => {
    const { data } = await apiClient.get<AuthMeRead>("/auth/me");
    return data;
  },
  changeOwnPassword: async (payload: AuthChangePasswordRequest): Promise<void> => {
    await apiClient.put("/auth/me/password", payload);
  },
  getPasswordPolicy: async (): Promise<PasswordPolicyRead> => {
    const { data } = await apiClient.get<PasswordPolicyRead>("/auth/password-policy");
    return data;
  },
};
