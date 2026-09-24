import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import { authApi } from "../api/auth";
import { apiClient } from "../api/client";
import type { AuthMeRead } from "../types/auth";

interface AuthState {
  token: string | null;
  user: AuthMeRead | null;
  mustChangePassword: boolean;
  rememberMe: boolean;

  login: (username: string, password: string, rememberMe: boolean) => Promise<void>;
  logout: () => Promise<void>;
  fetchMe: () => Promise<void>;
  changeOwnPassword: (oldPwd: string, newPwd: string) => Promise<void>;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      user: null,
      mustChangePassword: false,
      rememberMe: false,

      login: async (username, password, rememberMe) => {
        const res = await authApi.login({ username, password });
        set({
          token: res.accessToken,
          mustChangePassword: res.mustChangePassword,
          rememberMe,
        });
        await get().fetchMe();
      },

      logout: async () => {
        // 静默吞所有错误：改密后当前 token 已被吊销，调用必然 401
        try {
          await authApi.logout();
        } catch {
          /* noop */
        }
        set({ token: null, user: null, mustChangePassword: false });
        delete apiClient.defaults.headers.common.Authorization;
      },

      fetchMe: async () => {
        const me = await authApi.getMe();
        set({ user: me });
      },

      changeOwnPassword: async (oldPwd, newPwd) => {
        // 后端已吊销该用户所有 session（含当前）；调 logout 会收到 401，静默吞
        await authApi.changeOwnPassword({ oldPassword: oldPwd, newPassword: newPwd });
        await get().logout();
      },
    }),
    {
      name: "qa-system-auth",
      // 持久化用 localStorage（sessionStorage 在 tab 关闭后丢失，dev 调试体验差）。
      // rememberMe 字段仍保留在 state 上，供后续按需扩展（按 tab 而非全局隔离）。
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({
        token: s.token,
        mustChangePassword: s.mustChangePassword,
        rememberMe: s.rememberMe,
      }),
    },
  ),
);
