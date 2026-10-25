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
    // fix/login-401-white-screen（2026-10-25）：mock 的必须是拦截器真实 reject 的
    // AppError 形状（code/status/detail，无 .response）。旧 mock 是原始 axios 形状
    // { response: { status: 401, data: { detail: { code } } } } —— 生产链路里不存在，
    // 假绿掩盖了 LoginPage 永远弹「网络异常」的 bug。
    (authApi.login as any).mockRejectedValue(
      Object.assign(new Error("用户名或密码错误"), {
        code: "UNKNOWN",
        status: 401,
        detail: null,
      })
    );
    const user = userEvent.setup();
    render(<MemoryRouter><LoginPage /></MemoryRouter>);
    await user.type(screen.getByLabelText("用户名"), "admin");
    await user.type(screen.getByLabelText("密码"), "wrong");
    await user.click(screen.getByRole("button", { name: /登\s?录/ }));
    await waitFor(() => {
      expect(screen.getByText("用户名或密码错误")).toBeTruthy();
    });
  });

  it("429 显示 rateLimited", async () => {
    (authApi.login as any).mockRejectedValue(
      Object.assign(new Error("请求过于频繁"), { code: "UNKNOWN", status: 429, detail: null })
    );
    const user = userEvent.setup();
    render(<MemoryRouter><LoginPage /></MemoryRouter>);
    await user.type(screen.getByLabelText("用户名"), "admin");
    await user.type(screen.getByLabelText("密码", { selector: "input" }), "wrong");
    await user.click(screen.getByRole("button", { name: /登\s?录/ }));
    await waitFor(() => {
      expect(screen.getByText("尝试次数过多，请稍后再试")).toBeTruthy();
    });
  });

  it("非 401/429 错误显示 networkError 兜底", async () => {
    (authApi.login as any).mockRejectedValue(
      Object.assign(new Error("boom"), { code: "UNKNOWN", status: 0, detail: null })
    );
    const user = userEvent.setup();
    render(<MemoryRouter><LoginPage /></MemoryRouter>);
    await user.type(screen.getByLabelText("用户名"), "admin");
    await user.type(screen.getByLabelText("密码", { selector: "input" }), "wrong");
    await user.click(screen.getByRole("button", { name: /登\s?录/ }));
    await waitFor(() => {
      expect(screen.getByText("网络异常，请稍后重试")).toBeTruthy();
    });
  });
});
