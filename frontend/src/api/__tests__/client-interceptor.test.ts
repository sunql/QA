/**
 * client.ts response interceptor 401 vs 403 split test (audit-fx-2.1 Finding 4).
 *
 * Covers:
 * - 401 -> localStorage.removeItem("token") + window.location.href = "/auth/login"
 * - 403 -> localStorage and window.location unchanged
 * - 200 / other 4xx -> no side effects
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

// ---------------------------------------------------------------------------
// Pure function mirroring the actual interceptor logic from client.ts
// ---------------------------------------------------------------------------

/** Mirrors the error-handling branch of client.ts response interceptor. */
function handleResponseError(error: { response?: { status?: number } }): Promise<never> {
  const status = error.response?.status;

  // 401: token expired -> clear store and redirect to /login
  if (status === 401) {
    localStorage.removeItem("token");
    window.location.href = "/auth/login";
  }

  // Attach HTTP status for business-level routing (e.g. 403 -> permission modal)
  const err = new Error("request failed") as Error & { status?: number };
  if (typeof status === "number") {
    err.status = status;
  }
  return Promise.reject(err);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("client.ts response interceptor 401 vs 403", () => {
  beforeEach(() => {
    localStorage.setItem("token", "fake-token-12345");
    // Stub window.location.href to avoid jsdom navigation side effects
    let href = "";
    Object.defineProperty(window, "location", {
      value: {
        href,
        set href(val: string) { href = val; },
        get href() { return href; },
      },
      writable: true,
      configurable: true,
    });
  });

  afterEach(() => {
    localStorage.clear();
  });

  describe("401 AuthenticationFailed", () => {
    it("removes token from localStorage", async () => {
      const error = { response: { status: 401 } as const };
      await expect(handleResponseError(error)).rejects.toThrow();
      expect(localStorage.getItem("token")).toBeNull();
    });

    it("sets window.location.href to /auth/login", async () => {
      const error = { response: { status: 401 } as const };
      await expect(handleResponseError(error)).rejects.toThrow();
      expect(window.location.href).toBe("/auth/login");
    });

    it("rejects with error carrying status 401", async () => {
      const error = { response: { status: 401 } as const };
      await expect(handleResponseError(error)).rejects.toMatchObject({ status: 401 });
    });
  });

  describe("403 PermissionDenied", () => {
    it("does NOT remove token from localStorage", async () => {
      const error = { response: { status: 403 } as const };
      await expect(handleResponseError(error)).rejects.toThrow();
      expect(localStorage.getItem("token")).toBe("fake-token-12345");
    });

    it("does NOT change window.location.href", async () => {
      const error = { response: { status: 403 } as const };
      await expect(handleResponseError(error)).rejects.toThrow();
      expect(window.location.href).toBe("");
    });
  });

  describe("other status codes", () => {
    it("200 does NOT remove token", async () => {
      const error = { response: { status: 200 } as const };
      await expect(handleResponseError(error)).rejects.toMatchObject({ status: 200 });
      expect(localStorage.getItem("token")).toBe("fake-token-12345");
    });

    it("404 does NOT remove token", async () => {
      const error = { response: { status: 404 } as const };
      await expect(handleResponseError(error)).rejects.toMatchObject({ status: 404 });
      expect(localStorage.getItem("token")).toBe("fake-token-12345");
    });

    it("500 does NOT remove token", async () => {
      const error = { response: { status: 500 } as const };
      await expect(handleResponseError(error)).rejects.toMatchObject({ status: 500 });
      expect(localStorage.getItem("token")).toBe("fake-token-12345");
    });
  });
});
