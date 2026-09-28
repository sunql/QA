import { describe, it, expect, vi } from "vitest";
import { authApi } from "../api/auth";
import { apiClient } from "../api/client";

vi.mock("../api/client", () => ({
  apiClient: {
    post: vi.fn().mockResolvedValue({ data: { accessToken: "t" } }),
    get: vi.fn().mockResolvedValue({ data: { minLength: 8 } }),
    put: vi.fn().mockResolvedValue({}),
  },
}));

describe("authApi", () => {
  it("login POST /auth/login", async () => {
    const res = await authApi.login({ username: "a", password: "b" });
    expect(apiClient.post).toHaveBeenCalledWith("/auth/login", { username: "a", password: "b" });
    expect(res.accessToken).toBe("t");
  });

  it("getPasswordPolicy GET /auth/password-policy", async () => {
    const res = await authApi.getPasswordPolicy();
    expect(apiClient.get).toHaveBeenCalledWith("/auth/password-policy");
    expect(res.minLength).toBe(8);
  });

  it("changeOwnPassword PUT /auth/me/password", async () => {
    await authApi.changeOwnPassword({ oldPassword: "a", newPassword: "b" });
    expect(apiClient.put).toHaveBeenCalledWith("/auth/me/password", { oldPassword: "a", newPassword: "b" });
  });
});
