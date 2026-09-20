// authStore（feat-user-auth，2026-09-20）
//
// Zustand store 持有：token、user、rememberMe 标志。
// 持久化策略：
//   - rememberMe=true → localStorage（"qa-system-auth" key）
//   - rememberMe=false → sessionStorage（同 key，但会话级，关闭 tab 即清）
//
// 不变量：
//   - token 与 user 必须同生命周期：清 token 时一起清 user。
//   - logout() 必须先调 /api/v1/auth/logout 再清 store（401 静默吞）。
//   - handleAuthFailure()：401 拦截器调用 → 清 store + 抛"unauthenticated"事件。
//     React 层通过 useAuthHydration 或 RequireAuth 监听此事件重定向。

import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";

import type { AuthMeRead, UserSummary } from "../types/auth";
import { authApi } from "../api/auth";

const STORAGE_KEY = "qa-system-auth";

export interface AuthState {
  token: string | null;
  user: UserSummary | null;
  me: AuthMeRead | null;
  /** 登录响应里直接拿到的 mustChangePassword，避免异步 /me 来之前误判。 */
  mustChangePassword: boolean;
  rememberMe: boolean;
  /** 上次拉 /me 失败时间戳（避免 401 后无限重试） */
  lastMeFailedAt: number | null;

  // ----- actions -----
  /** 写入登录结果；rememberMe 控制持久化 backend。 */
  setLoginResult: (payload: {
    token: string;
    user: UserSummary;
    mustChangePassword: boolean;
    rememberMe: boolean;
  }) => void;
  /** 写入 /me 拉取结果（profile 页面、UserMenu 等）。 */
  setMe: (me: AuthMeRead) => void;
  /** 401 拦截器调用：清 store（不调后端）。 */
  clear: () => void;
  /** 用户主动登出：先调后端（吞 401）再清 store。 */
  logout: () => Promise<void>;
  /** 标记 /me 失败（避免循环重试）。 */
  markMeFailed: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      user: null,
      me: null,
      mustChangePassword: false,
      rememberMe: true,
      lastMeFailedAt: null,

      setLoginResult: ({ token, user, mustChangePassword, rememberMe }) => {
        set({
          token,
          user,
          me: null,
          mustChangePassword,
          rememberMe,
          lastMeFailedAt: null,
        });
      },

      setMe: (me) => {
        // 同步 mustChangePassword：以最新 /me 为准（可能后台 admin 已重置密码）
        set({ me, mustChangePassword: me.mustChangePassword });
      },

      clear: () => {
        set({
          token: null,
          user: null,
          me: null,
          mustChangePassword: false,
          lastMeFailedAt: Date.now(),
        });
      },

      logout: async () => {
        const token = get().token;
        if (token) {
          try {
            await authApi.logout();
          } catch {
            // 401 / 网络错误静默吞 —— 本地状态一定要清
          }
        }
        set({
          token: null,
          user: null,
          me: null,
          mustChangePassword: false,
          lastMeFailedAt: Date.now(),
        });
      },

      markMeFailed: () => {
        set({ lastMeFailedAt: Date.now() });
      },
    }),
    {
      name: STORAGE_KEY,
      // 当前实现：默认 localStorage（rememberMe=true）。
      // sessionStorage 路径留待后续：记得切换时迁移部分字段，避免多 tab 串号。
      storage: createJSONStorage(() => localStorage),
      // 不持久化 me / lastMeFailedAt —— /me 永远从后端拉最新
      // mustChangePassword 必须持久化 —— 硬刷新后仍能强制跳 /change-password
      partialize: (state) => ({
        token: state.token,
        user: state.user,
        mustChangePassword: state.mustChangePassword,
        rememberMe: state.rememberMe,
      }),
    },
  ),
);

// 选择器
export const selectIsAuthenticated = (s: AuthState): boolean =>
  Boolean(s.token) && Boolean(s.user);

export const selectMustChangePassword = (s: AuthState): boolean =>
  Boolean(s.mustChangePassword) || Boolean(s.me?.mustChangePassword);
