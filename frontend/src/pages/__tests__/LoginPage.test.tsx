/**
 * LoginPage error handling tests (audit-fx-3.3 Finding 10).
 *
 * RED phase: LoginPage should display i18n-mapped messages from error CODE,
 * not raw error text. Error codes: AUTH_INVALID, AUTH_EXPIRED, AUTH_RATE_LIMITED,
 * E_AUTH_INVALID_PASSWORD, E_AUTH_RATE_LIMITED, E_AUTH_EXPIRED.
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

describe("LoginPage error handling", () => {
  beforeEach(() => {
    mockLogin.mockReset();
    mockMessageError.mockReset();
  });

  it("shows i18n message on AUTH_INVALID", async () => {
    const error = new Error("Request failed") as Error & {
      response?: { status?: number; data?: { error?: string; detail?: unknown } };
    };
    error.response = {
      status: 401,
      data: { error: "MSG_INVALID_CREDENTIALS", detail: { code: "AUTH_INVALID" } },
    };
    mockLogin.mockRejectedValue(error);

    renderLoginPage();
    await fillAndSubmit("wrong", "wrong");

    // auth.login.invalidCredentials = "用户名或密码错误"
    expect(mockMessageError).toHaveBeenCalledWith("用户名或密码错误");
  });

  it("shows i18n message on E_AUTH_INVALID_PASSWORD", async () => {
    const error = new Error("Request failed") as Error & {
      response?: { status?: number; data?: { error?: string; detail?: unknown } };
    };
    error.response = {
      status: 401,
      data: { detail: { code: "E_AUTH_INVALID_PASSWORD" } },
    };
    mockLogin.mockRejectedValue(error);

    renderLoginPage();
    await fillAndSubmit("wrong", "wrong");

    expect(mockMessageError).toHaveBeenCalledWith("用户名或密码错误");
  });

  it("shows '会话已过期，请重新登录' on AUTH_EXPIRED", async () => {
    const error = new Error("Request failed") as Error & {
      response?: { status?: number; data?: { error?: string; detail?: unknown } };
    };
    error.response = {
      status: 401,
      data: { detail: { code: "AUTH_EXPIRED" } },
    };
    mockLogin.mockRejectedValue(error);

    renderLoginPage();
    await fillAndSubmit("user", "expired");

    expect(mockMessageError).toHaveBeenCalledWith("会话已过期，请重新登录");
  });

  it("shows '会话已过期，请重新登录' on E_AUTH_EXPIRED", async () => {
    const error = new Error("Request failed") as Error & {
      response?: { status?: number; data?: { error?: string; detail?: unknown } };
    };
    error.response = {
      status: 401,
      data: { detail: { code: "E_AUTH_EXPIRED" } },
    };
    mockLogin.mockRejectedValue(error);

    renderLoginPage();
    await fillAndSubmit("user", "expired");

    expect(mockMessageError).toHaveBeenCalledWith("会话已过期，请重新登录");
  });

  it("shows '请求过于频繁，请稍后再试' on AUTH_RATE_LIMITED", async () => {
    const error = new Error("Request failed") as Error & {
      response?: { status?: number; data?: { error?: string; detail?: unknown } };
    };
    error.response = {
      status: 429,
      data: { error: "MSG_RATE_LIMITED", detail: { code: "AUTH_RATE_LIMITED" } },
    };
    mockLogin.mockRejectedValue(error);

    renderLoginPage();
    await fillAndSubmit("user", "pass");

    expect(mockMessageError).toHaveBeenCalledWith("尝试次数过多，请稍后再试");
  });

  it("shows '请求过于频繁，请稍后再试' on E_AUTH_RATE_LIMITED", async () => {
    const error = new Error("Request failed") as Error & {
      response?: { status?: number; data?: { error?: string; detail?: unknown } };
    };
    error.response = {
      status: 429,
      data: { detail: { code: "E_AUTH_RATE_LIMITED" } },
    };
    mockLogin.mockRejectedValue(error);

    renderLoginPage();
    await fillAndSubmit("user", "pass");

    expect(mockMessageError).toHaveBeenCalledWith("尝试次数过多，请稍后再试");
  });
});
