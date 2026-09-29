import { describe, it, expect, vi, beforeEach } from "vitest";
import { useAuthStore } from "../stores/authStore";
import { authApi } from "../api/auth";

vi.mock("../api/auth");
vi.mock("../api/client", () => ({
  apiClient: { defaults: { headers: { common: {} } } },
}));

describe("authStore", () => {
  beforeEach(() => {
    useAuthStore.setState({ token: null, user: null, mustChangePassword: false, rememberMe: false });
  });

  it("login 成功设置 token + mustChangePassword + 调 fetchMe", async () => {
    (authApi.login as any).mockResolvedValue({
      accessToken: "t1", tokenType: "Bearer", expiresIn: 3600,
      mustChangePassword: true, user: { id: 1, username: "admin", displayName: "Admin", email: null, roles: [], organizations: [] },
    });
    (authApi.getMe as any).mockResolvedValue({ id: 1, username: "admin" });
    await useAuthStore.getState().login("admin", "Admin@123", true);
    expect(useAuthStore.getState().token).toBe("t1");
    expect(useAuthStore.getState().mustChangePassword).toBe(true);
    expect(useAuthStore.getState().user).toEqual({ id: 1, username: "admin" });
  });

  it("logout 静默吞 401", async () => {
    useAuthStore.setState({ token: "t1" });
    (authApi.logout as any).mockRejectedValue(new Error("401"));
    await useAuthStore.getState().logout();
    expect(useAuthStore.getState().token).toBeNull();
  });

  it("changeOwnPassword 成功后清态", async () => {
    useAuthStore.setState({ token: "t1" });
    (authApi.changeOwnPassword as any).mockResolvedValue(undefined);
    (authApi.logout as any).mockRejectedValue(new Error("401"));
    await useAuthStore.getState().changeOwnPassword("OldPwd1", "NewPwd1");
    expect(useAuthStore.getState().token).toBeNull();
  });
});
