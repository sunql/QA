/**
 * client.ts response interceptor 401 vs 403 split test (audit-fx-2.1 Finding 4;
 * 2026-10-25 fix/login-401-white-screen 重写为测真实 createHttpClient 链路).
 *
 * 旧版本在测试文件里复刻了一份拦截器逻辑（假绿：真实现坏了照样绿），
 * 现在通过 axios adapter 桩把 401/403 响应喂给真实拦截器。
 *
 * Covers:
 * - 401 -> 清 token（localStorage + authStore）+ 拒绝携带 AppError，
 *   但 **不做 window.location.href 硬跳转**（掉线重定向由 RequireAuth 软跳转负责；
 *   登录失败由 LoginPage 自己 toast——旧行为硬跳 /auth/login 白屏已删）
 * - 403 -> localStorage / store / location 均不动
 */

import { describe, expect, it, beforeEach, afterEach } from "vitest";
import { AxiosError } from "axios";
import type { InternalAxiosRequestConfig } from "axios";
import { createHttpClient } from "../client";
import { useAuthStore } from "../../stores/authStore";

/** 造一个拒绝式 adapter：模拟后端返回给定 status + data。 */
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

describe("client.ts response interceptor 401 vs 403（真链路）", () => {
  // location stub：记录 href 赋值，断言「没有发生硬跳转」；afterEach 恢复原描述符。
  let assignedHrefs: string[];
  let locationDescriptor: PropertyDescriptor | undefined;
  let client: ReturnType<typeof createHttpClient>;

  beforeEach(() => {
    localStorage.setItem("token", "fake-token-12345");
    useAuthStore.setState({
      token: "fake-token-12345",
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
    useAuthStore.setState({ token: null, user: null, mustChangePassword: false });
  });

  describe("401 AuthenticationFailed", () => {
    it("removes token from localStorage", async () => {
      client.defaults.adapter = makeRejectingAdapter(401, { detail: { code: "AUTH_INVALID" } });
      await expect(client.get("/anything")).rejects.toMatchObject({ status: 401 });
      expect(localStorage.getItem("token")).toBeNull();
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

  describe("403 PermissionDenied", () => {
    it("does NOT remove token from localStorage", async () => {
      client.defaults.adapter = makeRejectingAdapter(403, { detail: { code: "FORBIDDEN" } });
      await expect(client.get("/anything")).rejects.toMatchObject({ status: 403 });
      expect(localStorage.getItem("token")).toBe("fake-token-12345");
      expect(useAuthStore.getState().token).toBe("fake-token-12345");
    });

    it("does NOT change window.location.href", async () => {
      client.defaults.adapter = makeRejectingAdapter(403, { detail: { code: "FORBIDDEN" } });
      await expect(client.get("/anything")).rejects.toMatchObject({ status: 403 });
      expect(assignedHrefs).toEqual([]);
    });
  });
});
