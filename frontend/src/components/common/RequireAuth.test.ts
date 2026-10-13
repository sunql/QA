import { describe, it, expect } from "vitest";
import { isAllowedWhenMustChange } from "./RequireAuth";

describe("isAllowedWhenMustChange", () => {
  it("returns true for exact match /auth/change-password", () => {
    expect(isAllowedWhenMustChange("/auth/change-password")).toBe(true);
  });

  it("returns true for exact match /auth/login", () => {
    expect(isAllowedWhenMustChange("/auth/login")).toBe(true);
  });

  it("returns true for sub-path /auth/change-password/foo", () => {
    expect(isAllowedWhenMustChange("/auth/change-password/foo")).toBe(true);
  });

  it("returns false for /auth/change-password-v2 (prefix false-match regression)", () => {
    expect(isAllowedWhenMustChange("/auth/change-password-v2")).toBe(false);
  });

  it("returns false for unrelated path /api/v1/models", () => {
    expect(isAllowedWhenMustChange("/api/v1/models")).toBe(false);
  });
});
