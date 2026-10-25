import { describe, it, expect } from "vitest";
import { isAllowedWhenMustChange } from "./RequireAuth";

// fix/login-401-white-screen（2026-10-25）：白名单路径必须与 App.tsx 真实路由一致
// （/change-password、/login）。旧值 /auth/* 是不存在的路径，mustChangePassword
// 流程会被自己重定向到白屏。
describe("isAllowedWhenMustChange", () => {
  it("returns true for exact match /change-password（App.tsx 真实路由）", () => {
    expect(isAllowedWhenMustChange("/change-password")).toBe(true);
  });

  it("returns true for exact match /login（App.tsx 真实路由）", () => {
    expect(isAllowedWhenMustChange("/login")).toBe(true);
  });

  it("returns true for sub-path /change-password/foo", () => {
    expect(isAllowedWhenMustChange("/change-password/foo")).toBe(true);
  });

  it("returns false for /change-password-v2 (prefix false-match regression)", () => {
    expect(isAllowedWhenMustChange("/change-password-v2")).toBe(false);
  });

  it("returns false for legacy /auth/change-password（不存在的旧路径）", () => {
    expect(isAllowedWhenMustChange("/auth/change-password")).toBe(false);
  });

  it("returns false for legacy /auth/login（不存在的旧路径）", () => {
    expect(isAllowedWhenMustChange("/auth/login")).toBe(false);
  });

  it("returns false for unrelated path /api/v1/models", () => {
    expect(isAllowedWhenMustChange("/api/v1/models")).toBe(false);
  });
});
