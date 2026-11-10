import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useRestoreUser } from "../hooks/useRestoreUser";
import { useAuthStore } from "../stores/authStore";
import type { AuthMeRead } from "../types/auth";

const ADMIN_USER: AuthMeRead = {
  id: 1,
  username: "admin",
  displayName: "管理员",
  email: null,
  enabled: true,
  mustChangePassword: false,
  roles: ["admin"],
  organizations: [],
  tenantId: "default",
  lastLoginAt: null,
};

const authApi = vi.hoisted(() => ({
  getMe: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
  changeOwnPassword: vi.fn(),
  getPasswordPolicy: vi.fn(),
}));
vi.mock("../api/auth", () => ({ authApi }));

describe("useRestoreUser", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ token: null, user: null, mustChangePassword: false });
  });

  it("有 token 但 user 为空（刷新场景）→ 拉取 /auth/me 并写入 store", async () => {
    authApi.getMe.mockResolvedValue(ADMIN_USER);
    useAuthStore.setState({ token: "t-1", user: null });

    renderHook(() => useRestoreUser());

    await waitFor(() => {
      expect(useAuthStore.getState().user).toEqual(ADMIN_USER);
    });
    expect(authApi.getMe).toHaveBeenCalledTimes(1);
  });

  it("user 已存在 → 不重复请求", () => {
    useAuthStore.setState({ token: "t-1", user: ADMIN_USER });

    renderHook(() => useRestoreUser());

    expect(authApi.getMe).not.toHaveBeenCalled();
  });

  it("没有 token → 不请求（登录页）", () => {
    renderHook(() => useRestoreUser());

    expect(authApi.getMe).not.toHaveBeenCalled();
  });

  it("getMe 失败 → 吞错不抛出（401 已由拦截器处理）", async () => {
    authApi.getMe.mockRejectedValue(new Error("401"));
    useAuthStore.setState({ token: "t-expired", user: null });

    renderHook(() => useRestoreUser());

    await waitFor(() => {
      expect(authApi.getMe).toHaveBeenCalledTimes(1);
    });
    expect(useAuthStore.getState().user).toBeNull();
  });
});
