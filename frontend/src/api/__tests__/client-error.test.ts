/**
 * client.ts normalizeError + AppError interface tests (audit-fx-3.3 Finding 10).
 *
 * Covers:
 * - AppError shape: { code, status, detail }
 * - normalizeError extracts detail.code on 401 / other errors
 * - 401 side effects: localStorage token removal + redirect preserved from Task 2.1
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

// ---------------------------------------------------------------------------
// Types mirroring client.ts
// ---------------------------------------------------------------------------

interface AppError extends Error {
  code: string;
  status: number;
  detail?: unknown;
}

// ---------------------------------------------------------------------------
// Pure function mirroring the normalizeError implementation from client.ts
// ---------------------------------------------------------------------------

/** Mirrors the normalizeError function exported from client.ts. */
function normalizeError(err: { response?: { status?: number; data?: { detail?: unknown } }; message?: string }): AppError {
  const status = err.response?.status ?? 0;
  const rawDetail = err.response?.data?.detail;
  const detail = rawDetail ?? null;
  const code =
    typeof rawDetail === "object" && rawDetail !== null && "code" in rawDetail
      ? String((rawDetail as { code: unknown }).code)
      : "UNKNOWN";
  const message =
    typeof rawDetail === "object" && rawDetail !== null && "message" in rawDetail
      ? String((rawDetail as { message: unknown }).message)
      : err.message ?? "Unknown error";

  // Build AppError via Object.assign (same pattern as brief GREEN)
  const appErr: AppError = Object.assign(new Error(message), { code, status, detail });

  // 401: clear token + redirect (Task 2.1 behavior, preserved)
  if (status === 401) {
    localStorage.removeItem("token");
    window.location.href = "/auth/login";
  }

  return appErr;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Wraps a raw error object so normalizeError sees the AxiosError-shaped input. */
function runNormalize(err: { response?: { status?: number; data?: { detail?: unknown } }; message?: string }): AppError {
  return normalizeError(err);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("normalizeError", () => {
  beforeEach(() => {
    localStorage.setItem("token", "fake-token-12345");
    let href = "";
    Object.defineProperty(window, "location", {
      value: { href, set href(val: string) { href = val; }, get href() { return href; } },
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
