/**
 * LoginPage error handling tests (audit-fx-3.3 Finding 10;
 * 2026-10-25 fix/login-401-white-screen 重写为 status 契约).
 *
 * 旧版本 mock 的是原始 axios 形状 `{ response: { data: { detail: { code } } } }`
 * —— 拦截器真实 reject 的是 normalizeError 产出的 AppError（code/status/detail，
 * 无 .response），且后端登录失败（用户不存在/密码错/账号禁用）统一 401 防枚举，
 * 不存在 AUTH_EXPIRED / detail.code 形状 —— 假绿。
 *
 * 新契约（LoginPage 按 AppError.status 分流）：
 * - 401 → auth.login.invalidCredentials（用户名或密码错误）
 * - 429 → auth.login.rateLimited（尝试次数过多，请稍后再试）
 * - 其余 → auth.login.networkError（网络异常，请稍后重试）
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../../i18n";
import LoginPage from "../LoginPage";

// ---------------------------------------------------------------------------
// Mock dependencies
// ---------------------------------------------------------------------------

// Mock antd message.error so we can assert on it in jsdom
// vi.mock is hoisted to top, so mock fn must use vi.hoisted
const mockMessageError = vi.hoisted(() => vi.fn());
vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  return {
    ...actual,
    message: {
      ...(actual.message ?? {}),
      error: mockMessageError,
    },
  };
});

// Stub authApi.login — login is a function, so authApi.login() calls it directly
// without going through apiClient. Each test sets up the mock to return/reject.
const mockLogin = vi.hoisted(() => vi.fn());
vi.mock("../../api/auth", () => ({
  authApi: { login: mockLogin },
}));

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

/** 构造拦截器真实 reject 的 AppError 形状（normalizeError 产物）。 */
function makeAppError(status: number, message = "Request failed"): Error {
  return Object.assign(new Error(message), {
    code: "UNKNOWN",
    status,
    detail: null,
  });
}

function renderLoginPage() {
  return render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <ConfigProvider locale={zhCN}>
          <LoginPage />
        </ConfigProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
}

async function fillAndSubmit(username: string, password: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/用户名/i), username);
  await user.type(screen.getByLabelText(/密码/i), password);
  await user.click(screen.getByRole("button", { name: /登 ?录/i }));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("LoginPage error handling（AppError status 契约）", () => {
  beforeEach(() => {
    mockLogin.mockReset();
    mockMessageError.mockReset();
  });

  it("401（后端登录失败统一防枚举）→ 用户名或密码错误", async () => {
    mockLogin.mockRejectedValue(makeAppError(401));

    renderLoginPage();
    await fillAndSubmit("wrong", "wrong");

    // auth.login.invalidCredentials = "用户名或密码错误"
    expect(mockMessageError).toHaveBeenCalledWith("用户名或密码错误");
  });

  it("429 限流 → 尝试次数过多，请稍后再试", async () => {
    mockLogin.mockRejectedValue(makeAppError(429));

    renderLoginPage();
    await fillAndSubmit("user", "pass");

    expect(mockMessageError).toHaveBeenCalledWith("尝试次数过多，请稍后再试");
  });

  it("网络异常（status=0）→ 网络异常，请稍后重试兜底", async () => {
    mockLogin.mockRejectedValue(makeAppError(0));

    renderLoginPage();
    await fillAndSubmit("user", "pass");

    expect(mockMessageError).toHaveBeenCalledWith("网络异常，请稍后重试");
  });

  it("服务器错误（status=500）→ 同样走 networkError 兜底", async () => {
    mockLogin.mockRejectedValue(makeAppError(500));

    renderLoginPage();
    await fillAndSubmit("user", "pass");

    expect(mockMessageError).toHaveBeenCalledWith("网络异常，请稍后重试");
  });
});
