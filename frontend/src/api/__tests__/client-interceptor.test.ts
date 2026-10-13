/**
 * client.ts response interceptor 401 vs 403 split test (audit-fx-2.1 Finding 4).
 *
 * Covers:
 * - 401 -> localStorage.removeItem("token") + window.location.href = "/auth/login"
 * - 403 -> localStorage and window.location unchanged
 * - 200 / other 4xx -> no side effects
 *
 * 驱动**真实拦截器**（mock `axios.create` 后从 `use()` 抓注册进去的 onRejected），
 * 不再抄一份实现自测 —— 抄件不会随 client.ts 变红，是零覆盖（同
 * client-error.test.ts 的问题，PR #9 引进）。
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import axios from "axios";
import { createHttpClient } from "../client";

// axios.create 换成可控实例：真实 createHttpClient() 把 onRejected 注册到它身上，
// 测试再从 use() 的调用参数里取出来直接调用。
vi.mock("axios", () => {
  const instance = {
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
    get: vi.fn(),
    post: vi.fn(),
  };
  return { default: { create: vi.fn(() => instance) } };
});

describe("client.ts response interceptor 401 vs 403", () => {
  let onRejected: (error: unknown) => Promise<never>;

  beforeEach(() => {
    localStorage.setItem("token", "fake-token-12345");
    // Stub window.location.href to avoid jsdom navigation side effects
    let href = "";
    Object.defineProperty(window, "location", {
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

    const instance = (axios.create as ReturnType<typeof vi.fn>)();
    (instance.interceptors.response.use as ReturnType<typeof vi.fn>).mockImplementation(
      (_onFulfilled: unknown, rejected: typeof onRejected) => {
        onRejected = rejected;
      }
    );
    createHttpClient();
  });

  afterEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  describe("401 AuthenticationFailed", () => {
    it("removes token from localStorage", async () => {
      const error = { response: { status: 401 } as const };
      await expect(onRejected(error)).rejects.toThrow();
      expect(localStorage.getItem("token")).toBeNull();
    });

    it("sets window.location.href to /auth/login", async () => {
      const error = { response: { status: 401 } as const };
      await expect(onRejected(error)).rejects.toThrow();
      expect(window.location.href).toBe("/auth/login");
    });

    it("rejects with error carrying status 401", async () => {
      const error = { response: { status: 401 } as const };
      await expect(onRejected(error)).rejects.toMatchObject({ status: 401 });
    });
  });

  describe("403 PermissionDenied", () => {
    it("does NOT remove token from localStorage", async () => {
      const error = { response: { status: 403 } as const };
      await expect(onRejected(error)).rejects.toThrow();
      expect(localStorage.getItem("token")).toBe("fake-token-12345");
    });

    it("does NOT change window.location.href", async () => {
      const error = { response: { status: 403 } as const };
      await expect(onRejected(error)).rejects.toThrow();
      expect(window.location.href).toBe("");
    });
  });

  describe("other status codes", () => {
    it("200 does NOT remove token", async () => {
      const error = { response: { status: 200 } as const };
      await expect(onRejected(error)).rejects.toMatchObject({ status: 200 });
      expect(localStorage.getItem("token")).toBe("fake-token-12345");
    });

    it("404 does NOT remove token", async () => {
      const error = { response: { status: 404 } as const };
      await expect(onRejected(error)).rejects.toMatchObject({ status: 404 });
      expect(localStorage.getItem("token")).toBe("fake-token-12345");
    });

    it("500 does NOT remove token", async () => {
      const error = { response: { status: 500 } as const };
      await expect(onRejected(error)).rejects.toMatchObject({ status: 500 });
      expect(localStorage.getItem("token")).toBe("fake-token-12345");
    });
  });

  // 拦截器把**已经算好**的 errMsg 传给 normalizeError（client.ts 调用点）。
  // 这条断言盖的是那个调用点的接线：把第二个实参 `errMsg` 去掉，rejected 的
  // AppError.message 会退回 "Unknown error" / err.message，而弹窗文案不变 ——
  // 即 PR #9 遗留的那对自相矛盾的用户提示。只测 normalizeError 本身盖不到它。
  describe("错误信封 error 文案 → AppError.message（与 toast 同源）", () => {
    it("404 信封 error 成为 rejected AppError 的 message", async () => {
      const error = {
        response: { status: 404, data: { error: "未找到该资源" } },
        message: "Request failed with status code 404",
      };
      await expect(onRejected(error)).rejects.toMatchObject({
        message: "未找到该资源",
      });
    });
  });
});
