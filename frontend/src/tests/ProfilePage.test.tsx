// ProfilePage — 个人中心（feat-user-auth，2026-09-20）：
// 改用 /auth/me 替代旧的 /users/me；测试相应改造。

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../i18n";
import { ProfilePage } from "../pages/ProfilePage";
import { useAuthStore } from "../stores/authStore";

const auth = vi.hoisted(() => ({
  me: vi.fn(),
}));

vi.mock("../api/auth", () => ({
  authApi: { me: auth.me, login: vi.fn(), logout: vi.fn(), changeOwnPassword: vi.fn() },
}));

const wrap = (ui: React.ReactNode) => (
  <I18nextProvider i18n={i18n}>{ui}</I18nextProvider>
);

describe("ProfilePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    i18n.changeLanguage("zh-CN");
    useAuthStore.setState({
      token: "test-token",
      user: {
        id: 1,
        username: "admin",
        displayName: "系统管理员",
        email: null,
        roles: ["admin"],
        organizations: ["IT"],
      },
      me: null,
      rememberMe: true,
      lastMeFailedAt: null,
    });
  });

  it("渲染 DB 用户身份（角色/组织/邮箱）", async () => {
    auth.me.mockResolvedValue({
      id: 1,
      username: "admin",
      displayName: "系统管理员",
      email: "admin@example.com",
      enabled: true,
      mustChangePassword: false,
      roles: ["admin"],
      organizations: ["IT"],
      tenantId: "default",
      lastLoginAt: "2026-09-20T07:00:00Z",
    });
    render(wrap(<ProfilePage />));
    await waitFor(() => {
      expect(screen.getByText("系统管理员")).toBeInTheDocument();
    });
    expect(screen.getByText("admin@example.com")).toBeInTheDocument();
    expect(screen.getByText("IT")).toBeInTheDocument();
  });

  it("邮箱为空时显示 (无) 占位", async () => {
    auth.me.mockResolvedValue({
      id: 1,
      username: "alice",
      displayName: "Alice",
      email: null,
      enabled: true,
      mustChangePassword: true,
      roles: [],
      organizations: [],
      tenantId: "default",
      lastLoginAt: null,
    });
    render(wrap(<ProfilePage />));
    await waitFor(() => {
      expect(screen.getByText("Alice")).toBeInTheDocument();
    });
    // roles / orgs / lastLoginAt 三处空值都展示 (无)
    expect(screen.getAllByText("（无）").length).toBeGreaterThanOrEqual(3);
  });
});
