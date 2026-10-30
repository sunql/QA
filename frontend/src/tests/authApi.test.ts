import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
// auth.ts 用的是 apiClient（client.ts 里 httpClient 的别名，同一实例）
vi.mock("../api/client", () => ({ apiClient: httpMock }));

import { authApi } from "../api/auth";

describe("api/auth", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("login POST /auth/login（透传 payload，返回 accessToken）", async () => {
    httpMock.post.mockResolvedValue({
      data: { accessToken: "tok-1", tokenType: "Bearer", expiresIn: 3600 },
    });
    const result = await authApi.login({ username: "alice", password: "s3cret" });
    expect(httpMock.post).toHaveBeenCalledWith("/auth/login", {
      username: "alice",
      password: "s3cret",
    });
    expect(result.accessToken).toBe("tok-1");
  });

  it("logout POST /auth/logout（无 payload）", async () => {
    httpMock.post.mockResolvedValue({});
    await authApi.logout();
    expect(httpMock.post).toHaveBeenCalledWith("/auth/logout");
  });

  it("getMe GET /auth/me", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 7, username: "alice" } });
    const result = await authApi.getMe();
    expect(httpMock.get).toHaveBeenCalledWith("/auth/me");
    expect(result.username).toBe("alice");
  });

  it("changeOwnPassword PUT /auth/me/password（透传新旧密码）", async () => {
    httpMock.put.mockResolvedValue({});
    await authApi.changeOwnPassword({ oldPassword: "old-pwd", newPassword: "new-pwd" });
    expect(httpMock.put).toHaveBeenCalledWith("/auth/me/password", {
      oldPassword: "old-pwd",
      newPassword: "new-pwd",
    });
  });

  it("getPasswordPolicy GET /auth/password-policy", async () => {
    httpMock.get.mockResolvedValue({ data: { minLength: 8 } });
    const result = await authApi.getPasswordPolicy();
    expect(httpMock.get).toHaveBeenCalledWith("/auth/password-policy");
    expect(result.minLength).toBe(8);
  });

  it("登录失败时把后端错误原样抛出（不吞异常）", async () => {
    httpMock.post.mockRejectedValue(new Error("Request failed with status code 401"));
    await expect(authApi.login({ username: "alice", password: "bad" })).rejects.toThrow(
      "401",
    );
  });
});
