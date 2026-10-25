/**
 * ChangePasswordPage tests.
 *
 * 2026-10-25（fix/login-401-white-screen 后续）：
 * - catch 按 AppError.status 分流（旧实现读 e.response.data.error——AppError
 *   上不存在该形状，旧密码错永远弹「网络异常」）
 * - 断言走 mock 的 message.error 而非 DOM 文本：antd message 渲染在
 *   document.body 的 portal 里，跨测试残留会让 getByText 撞到上一个用例的 toast
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import ChangePasswordPage from "../pages/ChangePasswordPage";
import { useAuthStore } from "../stores/authStore";
import { authApi } from "../api/auth";

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

vi.mock("../api/auth");

/** 构造拦截器真实 reject 的 AppError 形状（与 client-error.test 同口径）。 */
function makeAppError(status: number, message = "Request failed"): Error {
  return Object.assign(new Error(message), { code: "UNKNOWN", status, detail: null });
}

async function fillAndSubmit(oldPwd: string, newPwd: string, confirmPwd: string) {
  const user = userEvent.setup();
  render(<MemoryRouter><ChangePasswordPage /></MemoryRouter>);
  await user.type(screen.getByLabelText("当前密码"), oldPwd);
  await user.type(screen.getByLabelText("新密码"), newPwd);
  await user.type(screen.getByLabelText("确认新密码"), confirmPwd);
  await user.click(screen.getByRole("button", { name: "修改密码" }));
}

describe("ChangePasswordPage", () => {
  beforeEach(() => {
    mockMessageError.mockReset();
    (authApi.changeOwnPassword as unknown as ReturnType<typeof vi.fn>).mockReset();
    useAuthStore.setState({ token: "t" });
  });

  it("两次输入不一致显示 mismatch", async () => {
    await fillAndSubmit("OldPwd1", "NewPwd1234", "Different1234");
    expect(mockMessageError).toHaveBeenCalledWith("两次输入不一致");
  });

  it("前端弱密码预检显示 weak", async () => {
    await fillAndSubmit("OldPwd1", "weak", "weak");
    expect(mockMessageError).toHaveBeenCalledWith("密码至少 8 位且必须包含字母和数字");
  });

  it("旧密码错误（401 AppError）显示 oldWrong 且不清登录态", async () => {
    (authApi.changeOwnPassword as unknown as ReturnType<typeof vi.fn>).mockRejectedValue(
      makeAppError(401)
    );
    await fillAndSubmit("WrongOld1", "NewPwd1234", "NewPwd1234");
    expect(mockMessageError).toHaveBeenCalledWith("当前密码不正确");
    // 凭证校验失败 ≠ 会话过期：token 保持，页面仍在（配合拦截器豁免改密接口）
    expect(useAuthStore.getState().token).toBe("t");
    expect(screen.getByRole("button", { name: "修改密码" })).toBeTruthy();
  });

  it("后端 422（弱密码双保险）显示 weak", async () => {
    (authApi.changeOwnPassword as unknown as ReturnType<typeof vi.fn>).mockRejectedValue(
      makeAppError(422)
    );
    await fillAndSubmit("OldPwd1", "NewPwd1234", "NewPwd1234");
    expect(mockMessageError).toHaveBeenCalledWith("密码至少 8 位且必须包含字母和数字");
  });

  it("其余错误走 networkError 兜底", async () => {
    (authApi.changeOwnPassword as unknown as ReturnType<typeof vi.fn>).mockRejectedValue(
      makeAppError(500)
    );
    await fillAndSubmit("OldPwd1", "NewPwd1234", "NewPwd1234");
    expect(mockMessageError).toHaveBeenCalledWith("网络异常，请稍后重试");
  });
});
