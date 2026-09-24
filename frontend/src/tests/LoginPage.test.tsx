import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import LoginPage from "../pages/LoginPage";
import { useAuthStore } from "../stores/authStore";
import { authApi } from "../api/auth";

vi.mock("../api/auth");
// react-i18next 全局 mock 在 setup.ts

describe("LoginPage", () => {
  beforeEach(() => {
    useAuthStore.setState({ token: null, mustChangePassword: false });
  });

  it("已登录直接跳走", () => {
    useAuthStore.setState({ token: "t" });
    render(<MemoryRouter><LoginPage /></MemoryRouter>);
    // 不会渲染 form
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("登录成功 mustChangePassword=true 跳 /change-password", async () => {
    (authApi.login as any).mockResolvedValue({
      accessToken: "t1", mustChangePassword: true,
      user: { id: 1, username: "admin", displayName: "Admin" },
    });
    const user = userEvent.setup();
    render(<MemoryRouter><LoginPage /></MemoryRouter>);
    await user.type(screen.getByLabelText("用户名"), "admin");
    await user.type(screen.getByLabelText("密码"), "Admin@123");
    await user.click(screen.getByRole("button", { name: /登\s?录/ }));
    await waitFor(() => {
      expect(useAuthStore.getState().token).toBe("t1");
    });
  });

  it("401 显示 invalidCredentials", async () => {
    (authApi.login as any).mockRejectedValue({
      response: { status: 401, data: { error: "MSG_INVALID_CREDENTIALS" } },
    });
    const user = userEvent.setup();
    render(<MemoryRouter><LoginPage /></MemoryRouter>);
    await user.type(screen.getByLabelText("用户名"), "admin");
    await user.type(screen.getByLabelText("密码"), "wrong");
    await user.click(screen.getByRole("button", { name: /登\s?录/ }));
    await waitFor(() => {
      expect(screen.getByText("用户名或密码错误")).toBeTruthy();
    });
  });
});
