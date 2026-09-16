/** ProfilePage — 个人中心（2026-09-16）：身份展示 + 桩回退提示。 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../i18n";
import { ProfilePage } from "../pages/ProfilePage";

const api = vi.hoisted(() => ({
  fetchCurrentUserMe: vi.fn(),
}));

vi.mock("../api/userProfile", () => api);

const wrap = (ui: React.ReactNode) => (
  <I18nextProvider i18n={i18n}>{ui}</I18nextProvider>
);

describe("ProfilePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    i18n.changeLanguage("zh-CN");
  });

  it("渲染 DB 用户身份（角色/组织/来源）", async () => {
    api.fetchCurrentUserMe.mockResolvedValue({
      userId: "admin",
      displayName: "系统管理员",
      email: "admin@example.com",
      roleCodes: ["admin"],
      departmentCodes: ["IT"],
      dbUserId: 1,
    });
    render(wrap(<ProfilePage />));
    await waitFor(() => {
      expect(screen.getByText("系统管理员")).toBeInTheDocument();
    });
    expect(screen.getByText("admin@example.com")).toBeInTheDocument();
    expect(screen.getByText("数据库用户")).toBeInTheDocument();
    expect(screen.getByText("IT")).toBeInTheDocument();
  });

  it("桩回退身份显示提示 Tag 与空邮箱占位", async () => {
    api.fetchCurrentUserMe.mockResolvedValue({
      userId: "system",
      displayName: "system",
      email: null,
      roleCodes: ["user", "admin"],
      departmentCodes: [],
      dbUserId: null,
    });
    render(wrap(<ProfilePage />));
    await waitFor(() => {
      expect(screen.getByText("桩回退（未命中数据库用户）")).toBeInTheDocument();
    });
    // email / 组织为空时显示 "-" 占位（多处）
    expect(screen.getAllByText("-").length).toBeGreaterThanOrEqual(1);
  });
});
