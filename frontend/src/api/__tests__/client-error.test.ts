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

  // 后端 ErrorResponse(CamelModel)：{ success, error: string, detail: string|None, details }
  // DomainError 抛出的业务文案放在顶层 error，detail 恒为 null —— 不lift 顶层 error，
  // 页面就只能拿到 axios 的 "Request failed with status code 422"。
  describe("响应体顶层 error 的兜底提取", () => {
    it("lifts the top-level body error when detail carries no message", () => {
      const err = normalizeError(
        makeAxiosError(
          422,
          { success: false, error: "工具不存在：X", detail: null },
          "Request failed with status code 422",
        ),
      );
      expect(err.message).toBe("工具不存在：X");
      expect(err.code).toBe("UNKNOWN");
      expect(err.status).toBe(422);
    });

    it("prefers detail.message over the top-level body error", () => {
      const err = normalizeError(
        makeAxiosError(400, {
          error: "顶层文案",
          detail: { code: "TEST_ERROR", message: "detail 文案" },
        }),
      );
      expect(err.message).toBe("detail 文案");
      expect(err.code).toBe("TEST_ERROR");
    });

    it("falls back to the axios message when the body error is empty", () => {
      const err = normalizeError(
        makeAxiosError(500, { success: false, error: "", detail: null }, "Server error"),
      );
      expect(err.message).toBe("Server error");
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
