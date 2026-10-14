import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import { authApi } from "../api/auth";
import { apiClient } from "../api/client";
import { clearLastSessionIds } from "./persistChatUiState";
import { notifyUserSwitch } from "./userSwitch";
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
        // 登录成功即视为换人：清掉上一个登录会话留下的会话恢复指针**与内存里的对话**。
        // 放在 await 之后而不是函数开头 —— 口令错时老用户仍然登录着，不该丢掉他的恢复目标。
        // 只清 localStorage 不够：store 里的 messages 还在，而 enterChannel 的
        // 「同渠道且已有消息」早退会让新登录的人直接看到上一场对话。
        clearLastSessionIds();
        notifyUserSwitch();
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
        // 会话恢复指针只在同一个登录会话内有效：留着它，下一个人登录后会被
        // 自动恢复到上一个人的会话（自动恢复没有「点一下」这个人为闸门了）。
        // 内存里的对话同样作废：共享终端上，不刷新页面就换人时它就留在屏幕上。
        clearLastSessionIds();
        notifyUserSwitch();
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
      //
      // createJSONStorage 在调用时同步执行 getStorage() 取一次 storage 实例。
      // 包装为 try/throwing：测试环境（jsdom）若 localStorage 尚未挂载（setup.ts 跑得比
      // 模块顶层 import 晚），访问会抛 ReferenceError，createJSONStorage 内部 try/catch
      // 捕获后返回 undefined → persist 走「无 storage」分支（仅 warn 不抛错）。
      storage: createJSONStorage(() => {
        // 主动用 typeof 探测而非直接引用，避免 TS「未使用变量」+ ReferenceError 双重命中
        const ls = typeof window !== "undefined" ? window.localStorage : undefined;
        if (!ls) throw new Error("localStorage unavailable");
        return ls;
      }),
      partialize: (s) => ({
        token: s.token,
        mustChangePassword: s.mustChangePassword,
        rememberMe: s.rememberMe,
      }),
    },
  ),
);
