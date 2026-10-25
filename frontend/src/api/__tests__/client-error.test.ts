/**
 * client.ts normalizeError + AppError interface tests (audit-fx-3.3 Finding 10;
 * 2026-10-25 fix/login-401-white-screen 重写为 import 真实现).
 *
 * Covers:
 * - AppError shape: { code, status, detail }
 * - normalizeError extracts detail.code on 401 / other errors
 * - 401 NOT hard-navigating：normalizeError 只做归一化，不再做
 *   window.location.href 硬跳转（旧行为跳到不存在的 /auth/login 白屏，
 *   且页面卸载导致 LoginPage 的 AUTH_INVALID message.error 永远不执行）。
 *   401 的登录态清理/重定向由响应拦截器 + RequireAuth 软跳转负责
 *   （见 client-interceptor.test.ts，测真实 createHttpClient 链路）。
 */

import { describe, expect, it, beforeEach, afterEach } from "vitest";
import { normalizeError } from "../client";
import type { AxiosError } from "axios";
import type { ApiResponse } from "../../types/common";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** 构造 AxiosError 形状的输入（与拦截器传给 normalizeError 的一致）。 */
function makeAxiosError(
  status: number,
  data?: unknown,
  message = "Request failed"
): AxiosError<ApiResponse<unknown>> {
  return {
    response: { status, data },
    message,
  } as unknown as AxiosError<ApiResponse<unknown>>;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("normalizeError（真实现）", () => {
  // location stub：记录 href 赋值（jsdom 对导航赋值只打日志不抛错，
  // 必须自己 stub 才能断言「没有发生跳转」）；afterEach 恢复原描述符。
  let assignedHrefs: string[];
  let locationDescriptor: PropertyDescriptor | undefined;

  beforeEach(() => {
    localStorage.setItem("token", "fake-token-12345");
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
  });

  afterEach(() => {
    if (locationDescriptor) {
      Object.defineProperty(window, "location", locationDescriptor);
    }
    localStorage.clear();
  });

  describe("AppError shape", () => {
    it("returns an Error with code, status, and detail properties", () => {
      const err = normalizeError(makeAxiosError(400, { detail: { code: "TEST_ERROR", message: "test" } }));
      expect(err).toBeInstanceOf(Error);
      expect(err.code).toBe("TEST_ERROR");
      expect(err.status).toBe(400);
      expect(err.detail).toEqual({ code: "TEST_ERROR", message: "test" });
    });

    it("defaults code to UNKNOWN when detail has no code", () => {
      const err = normalizeError(makeAxiosError(500, { detail: "Internal error" }, "Server error"));
      expect(err.code).toBe("UNKNOWN");
      expect(err.status).toBe(500);
      expect(err.message).toBe("Server error");
    });

    it("defaults code to UNKNOWN when response has no detail", () => {
      const err = normalizeError(makeAxiosError(503, undefined, "Service unavailable"));
      expect(err.code).toBe("UNKNOWN");
      expect(err.status).toBe(503);
    });
  });

  describe("401 错误码提取", () => {
    it("extracts code AUTH_INVALID from detail", () => {
      const err = normalizeError(makeAxiosError(401, { detail: { code: "AUTH_INVALID" } }));
      expect(err.code).toBe("AUTH_INVALID");
      expect(err.status).toBe(401);
    });

    it("extracts code E_AUTH_INVALID_PASSWORD from detail", () => {
      const err = normalizeError(makeAxiosError(401, { detail: { code: "E_AUTH_INVALID_PASSWORD" } }));
      expect(err.code).toBe("E_AUTH_INVALID_PASSWORD");
      expect(err.status).toBe(401);
    });

    it("extracts code E_AUTH_RATE_LIMITED from detail", () => {
      const err = normalizeError(makeAxiosError(429, { detail: { code: "E_AUTH_RATE_LIMITED" } }));
      expect(err.code).toBe("E_AUTH_RATE_LIMITED");
      expect(err.status).toBe(429);
    });

    it("extracts code E_AUTH_EXPIRED from detail", () => {
      const err = normalizeError(makeAxiosError(401, { detail: { code: "E_AUTH_EXPIRED" } }));
      expect(err.code).toBe("E_AUTH_EXPIRED");
      expect(err.status).toBe(401);
    });
  });

  describe("401 不做硬跳转（fix/login-401-white-screen）", () => {
    it("does NOT navigate on 401 — LoginPage 的错误提示必须有机会执行", () => {
      // 旧实现：window.location.href = "/auth/login"（不存在的路由 → 白屏，
      // 且页面卸载吃掉 LoginPage 的 message.error）。
      normalizeError(makeAxiosError(401, { detail: { code: "AUTH_INVALID" } }));
      expect(assignedHrefs).toEqual([]);
    });

    it("does NOT touch localStorage on 401（登录态清理归响应拦截器管）", () => {
      normalizeError(makeAxiosError(401, { detail: { code: "AUTH_INVALID" } }));
      expect(localStorage.getItem("token")).toBe("fake-token-12345");
    });

    it("does NOT navigate on non-401 either", () => {
      normalizeError(makeAxiosError(403, { detail: { code: "FORBIDDEN" } }, "Forbidden"));
      expect(assignedHrefs).toEqual([]);
    });
  });
});
