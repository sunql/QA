/**
 * client.ts response interceptor 401 vs 403 split test (audit-fx-2.1 Finding 4;
 * 2026-10-25 fix/login-401-white-screen 重写为测真实 createHttpClient 链路).
 *
 * 旧版本在测试文件里复刻了一份拦截器逻辑（假绿：真实现坏了照样绿），
 * 现在通过 axios adapter 桩把 401/403 响应喂给真实拦截器。
 *
 * Covers:
 * - 401 -> 清 token（authStore + **持久层**）+ 拒绝携带 AppError，
 *   但 **不做 window.location.href 硬跳转**（掉线重定向由 RequireAuth 软跳转负责；
 *   登录失败由 LoginPage 自己 toast——旧行为硬跳 /auth/login 白屏已删）
 * - 403 -> 持久层 / store / location 均不动
 *
 * 2026-11-18（fix/auth-401-persist-cleanup）：
 *   旧版断言用的是 `localStorage["token"]` —— **全仓从来没有代码写过这个键**
 *   （唯一写 token 的是 zustand persist，键名 `qa-system-auth`，见 stores/authStore.ts），
 *   旧断言只证明了「测试自己造的假键被删了」，证明不了「过期 token 真被清了」。
 *   ⇒ 改成从 store 自身取 persist 键名，断言**真实的持久层**。
 */

import { describe, expect, it, beforeEach, afterEach } from "vitest";
import { AxiosError } from "axios";
import type { InternalAxiosRequestConfig } from "axios";
import { createHttpClient } from "../client";
import { useAuthStore } from "../../stores/authStore";

/** 造一个拒绝式 adapter：模拟后端返回给定 status + data（config.url 由 axios 填充）。 */
function makeRejectingAdapter(status: number, data: unknown) {
  return (config: InternalAxiosRequestConfig) => {
    const response = {
      data,
      status,
      statusText: String(status),
      headers: {},
      config,
    };
    return Promise.reject(
      new AxiosError("Request failed", AxiosError.ERR_BAD_RESPONSE, config, undefined, response)
    );
  };
}

/** 持久层键名从 store 自己取（单一事实源，不在测试里复写字面量）。 */
const PERSIST_KEY = useAuthStore.persist.getOptions().name ?? "";
const EXPIRED_TOKEN = "fake-token-12345";

/** 读持久层里存的 token（解析 zustand persist 的 {state:{...},version} 结构）。 */
function readPersistedToken(): string | null {
  const raw = localStorage.getItem(PERSIST_KEY);
  if (raw === null) return null;
  const parsed = JSON.parse(raw) as { state?: { token?: string | null } };
  return parsed.state?.token ?? null;
}

describe("client.ts response interceptor 401 vs 403（真链路）", () => {
  // location stub：记录 href 赋值，断言「没有发生硬跳转」；afterEach 恢复原描述符。
  let assignedHrefs: string[];
  let locationDescriptor: PropertyDescriptor | undefined;
  let client: ReturnType<typeof createHttpClient>;

  beforeEach(() => {
    // 经 store 写入 ⇒ zustand persist 同步落盘（真实持久层，键 = PERSIST_KEY）。
    useAuthStore.setState({
      token: EXPIRED_TOKEN,
      user: null,
      mustChangePassword: false,
    });
    assignedHrefs = [];
    locationDescriptor = Object.getOwnPropertyDescriptor(window, "location");
    Object.defineProperty(window, "location", {
      value: {
        set href(val: string) {
          assignedHrefs.push(val);
        },
        get href() {
          return "";
        },
      },
      writable: true,
      configurable: true,
    });
    client = createHttpClient();
  });

  afterEach(() => {
    if (locationDescriptor) {
      Object.defineProperty(window, "location", locationDescriptor);
    }
    localStorage.clear();
    useAuthStore.persist.clearStorage();
    useAuthStore.setState({ token: null, user: null, mustChangePassword: false });
  });

  describe("401 AuthenticationFailed", () => {
    it("持久层键名就是 authStore 的 persist name（防空扫描假绿）", () => {
      // 若 store 哪天去掉 persist，本文件的全部断言会退化成「对空键的断言」⇒ 必须钉住。
      expect(PERSIST_KEY).toBe("qa-system-auth");
    });

    it("清掉持久层里的过期 token（真实 persist 键）", async () => {
      // 前置：持久层里确实存着过期 token —— 否则下面的断言恒真（防空扫描假绿）。
      expect(readPersistedToken()).toBe(EXPIRED_TOKEN);

      client.defaults.adapter = makeRejectingAdapter(401, { detail: { code: "AUTH_INVALID" } });
      await expect(client.get("/anything")).rejects.toMatchObject({ status: 401 });

      expect(readPersistedToken()).toBeNull();
      // 更强的一层：过期 token 的字面量不得残留在持久层的任何形态里。
      expect(localStorage.getItem(PERSIST_KEY) ?? "").not.toContain(EXPIRED_TOKEN);
    });

    it("clears authStore token (RequireAuth 由此感知并软跳转 /login)", async () => {
      client.defaults.adapter = makeRejectingAdapter(401, { detail: { code: "AUTH_INVALID" } });
      await expect(client.get("/anything")).rejects.toMatchObject({ status: 401 });
      expect(useAuthStore.getState().token).toBeNull();
    });

    it("does NOT hard-navigate（旧实现跳 /auth/login 白屏，已删）", async () => {
      client.defaults.adapter = makeRejectingAdapter(401, { detail: { code: "AUTH_INVALID" } });
      await expect(client.get("/anything")).rejects.toMatchObject({ status: 401 });
      expect(assignedHrefs).toEqual([]);
    });

    it("rejects with AppError carrying code + status", async () => {
      client.defaults.adapter = makeRejectingAdapter(401, { detail: { code: "AUTH_INVALID" } });
      await expect(client.get("/anything")).rejects.toMatchObject({
        status: 401,
        code: "AUTH_INVALID",
      });
    });
  });

  describe("401 on 改密接口（凭证校验失败，非会话过期）", () => {
    it("does NOT clear token/store — 用户留在改密页自己提示", async () => {
      client.defaults.adapter = makeRejectingAdapter(
        401,
        { success: false, error: "MSG_OLD_PASSWORD_INCORRECT", detail: null }
      );
      await expect(client.put("/auth/me/password", {})).rejects.toMatchObject({ status: 401 });
      expect(readPersistedToken()).toBe(EXPIRED_TOKEN);
      expect(useAuthStore.getState().token).toBe(EXPIRED_TOKEN);
      expect(assignedHrefs).toEqual([]);
    });
  });

  // 反向守卫自检：location stub 的 setter / getter 自身也必须被真正执行过一次。
  // 否则上面几条「没有发生跳转」的断言可能是恒真 —— stub 根本没装上，
  // 任何 href 赋值都不会被记录，断言照样绿。
  describe("location stub 自检（反向守卫）", () => {
    it("set href 会记录赋值、get href 返回桩值（证明『未跳转』断言非恒真）", () => {
      // getter：读一次，触发 stub 的 get href
      expect(window.location.href).toBe("");
      // setter：赋一次，触发 stub 的 set href —— 赋值被记录进 assignedHrefs
      window.location.href = "/auth/login";
      expect(assignedHrefs).toEqual(["/auth/login"]);
    });
  });

  describe("403 PermissionDenied", () => {
    it("does NOT clear token — 持久层与 store 都保持原样", async () => {
      client.defaults.adapter = makeRejectingAdapter(403, { detail: { code: "FORBIDDEN" } });
      await expect(client.get("/anything")).rejects.toMatchObject({ status: 403 });
      expect(readPersistedToken()).toBe(EXPIRED_TOKEN);
      expect(useAuthStore.getState().token).toBe(EXPIRED_TOKEN);
    });

    it("does NOT change window.location.href", async () => {
      client.defaults.adapter = makeRejectingAdapter(403, { detail: { code: "FORBIDDEN" } });
      await expect(client.get("/anything")).rejects.toMatchObject({ status: 403 });
      expect(assignedHrefs).toEqual([]);
    });
  });
});
