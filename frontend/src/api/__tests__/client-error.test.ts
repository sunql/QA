/**
 * client.ts normalizeError + AppError interface tests (audit-fx-3.3 Finding 10).
 *
 * Covers:
 * - AppError shape: { code, status, detail }
 * - normalizeError extracts detail.code on 401 / other errors
 * - 401 side effects: localStorage token removal + redirect preserved from Task 2.1
 *
 * 直接 import **真实实现**。原先此处把 `normalizeError` 抄了一份再测那份抄件 ——
 * 真实现怎么改它都不会红（PR #9 引进；main 上真实实现把 404/网络异常的 message
 * 退化成 "Unknown error" 时，这份抄件照样全绿，等于零覆盖）。
 */

import { describe, expect, it, beforeEach, afterEach } from "vitest";
import { normalizeError, type AppError } from "../client";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type NormalizeInput = Parameters<typeof normalizeError>[0];

/** 以 AxiosError 形状喂真实实现（调用点不必写全 AxiosError 类型）。 */
function runNormalize(err: unknown, fallbackMessage?: string): AppError {
  return normalizeError(err as NormalizeInput, fallbackMessage);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("normalizeError", () => {
  beforeEach(() => {
    localStorage.setItem("token", "fake-token-12345");
    let href = "";
    Object.defineProperty(window, "location", {
      // 只留访问器：对象字面量里同时写 `href` 属性与 `get/set href` 会被 TS 判为
      // 重复标识（TS1119/TS2300），这正是本文件此前 build 报错的来源。
      value: {
        get href() {
          return href;
        },
        set href(val: string) {
          href = val;
        },
      },
      writable: true,
      configurable: true,
    });
  });

  afterEach(() => {
    localStorage.clear();
  });

  describe("AppError shape", () => {
    it("returns an Error with code, status, and detail properties", () => {
      const err = runNormalize({ response: { status: 400, data: { detail: { code: "TEST_ERROR", message: "test" } } } });
      expect(err).toBeInstanceOf(Error);
      expect(err.code).toBe("TEST_ERROR");
      expect(err.status).toBe(400);
      expect(err.detail).toEqual({ code: "TEST_ERROR", message: "test" });
    });

    it("defaults code to UNKNOWN when detail has no code", () => {
      const err = runNormalize({ response: { status: 500, data: { detail: "Internal error" } }, message: "Server error" });
      expect(err.code).toBe("UNKNOWN");
      expect(err.status).toBe(500);
      expect(err.message).toBe("Server error");
    });

    it("defaults code to UNKNOWN when response has no detail", () => {
      const err = runNormalize({ response: { status: 503 }, message: "Service unavailable" });
      expect(err.code).toBe("UNKNOWN");
      expect(err.status).toBe(503);
    });
  });

  // fallbackMessage：拦截器把它**已经算好**的 toast 文案传进来，让弹窗与
  // AppError.message 同源。少了下面两条断言，把 client.ts 的这个参数整个删掉
  // 套件依然全绿 —— 而这正是本次修复的**全部内容**（PR #9 遗留的 404 分歧：
  // 弹窗说「未找到」、页面拿到 "Unknown error"）。
  describe("fallbackMessage（toast 与 AppError.message 同源）", () => {
    it("detail 是字符串时，用调用方传入的 toast 文案作为 message", () => {
      const err = runNormalize(
        { response: { status: 404, data: { detail: "未找到" } } },
        "未找到",
      );
      expect(err.message).toBe("未找到");
    });

    it("对象形态 detail.message 优先于 fallbackMessage", () => {
      const err = runNormalize(
        { response: { status: 400, data: { detail: { code: "E_X", message: "资源不存在" } } } },
        "兜底文案",
      );
      expect(err.message).toBe("资源不存在");
    });
  });

  describe("interceptor returns AppError on 401", () => {
    it("extracts code AUTH_INVALID from detail", () => {
      const err = runNormalize({
        response: { status: 401, data: { detail: { code: "AUTH_INVALID" } } },
      });
      expect(err.code).toBe("AUTH_INVALID");
      expect(err.status).toBe(401);
    });

    it("extracts code E_AUTH_INVALID_PASSWORD from detail", () => {
      const err = runNormalize({
        response: { status: 401, data: { detail: { code: "E_AUTH_INVALID_PASSWORD" } } },
      });
      expect(err.code).toBe("E_AUTH_INVALID_PASSWORD");
      expect(err.status).toBe(401);
    });

    it("extracts code E_AUTH_RATE_LIMITED from detail", () => {
      const err = runNormalize({
        response: { status: 429, data: { detail: { code: "E_AUTH_RATE_LIMITED" } } },
      });
      expect(err.code).toBe("E_AUTH_RATE_LIMITED");
      expect(err.status).toBe(429);
    });

    it("extracts code E_AUTH_EXPIRED from detail", () => {
      const err = runNormalize({
        response: { status: 401, data: { detail: { code: "E_AUTH_EXPIRED" } } },
      });
      expect(err.code).toBe("E_AUTH_EXPIRED");
      expect(err.status).toBe(401);
    });

    it("removes token from localStorage on 401", () => {
      runNormalize({ response: { status: 401, data: { detail: { code: "AUTH_INVALID" } } } });
      expect(localStorage.getItem("token")).toBeNull();
    });

    it("sets window.location.href to /auth/login on 401", () => {
      runNormalize({ response: { status: 401, data: { detail: { code: "AUTH_INVALID" } } } });
      expect(window.location.href).toBe("/auth/login");
    });

    it("does NOT redirect on non-401", () => {
      runNormalize({ response: { status: 403, data: { detail: { code: "FORBIDDEN" } } }, message: "Forbidden" });
      expect(window.location.href).toBe("");
    });
  });
});
