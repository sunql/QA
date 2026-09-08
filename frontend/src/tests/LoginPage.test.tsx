import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import LoginPage from "../pages/LoginPage";
import { useAuthStore } from "../stores/authStore";
import { authApi } from "../api/auth";

vi.mock("../api/auth");
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

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
    await user.type(screen.getByLabelText("auth.login.username"), "admin");
    await user.type(screen.getByLabelText("auth.login.password"), "Admin@123");
    await user.click(screen.getByRole("button", { name: "auth.login.submit" }));
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
    await user.type(screen.getByLabelText("auth.login.username"), "admin");
    await user.type(screen.getByLabelText("auth.login.password"), "wrong");
    await user.click(screen.getByRole("button", { name: "auth.login.submit" }));
    await waitFor(() => {
      expect(screen.getByText("auth.login.invalidCredentials")).toBeTruthy();
    });
  });
});
