import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import ChangePasswordPage from "../pages/ChangePasswordPage";
import { useAuthStore } from "../stores/authStore";

vi.mock("../api/auth");
// react-i18next 全局 mock 在 setup.ts

describe("ChangePasswordPage", () => {
  beforeEach(() => {
    useAuthStore.setState({ token: "t" });
  });

  it("不匹配显示 mismatch", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><ChangePasswordPage /></MemoryRouter>);
    await user.type(screen.getByLabelText("auth.changePassword.oldPassword"), "OldPwd1");
    await user.type(screen.getByLabelText("auth.changePassword.newPassword"), "NewPwd1234");
    await user.type(screen.getByLabelText("auth.changePassword.confirmPassword"), "Different1234");
    await user.click(screen.getByRole("button", { name: "auth.changePassword.title" }));
    await waitFor(() => {
      expect(screen.getByText("auth.changePassword.mismatch")).toBeTruthy();
    });
  });

  it("弱密码显示 weak", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><ChangePasswordPage /></MemoryRouter>);
    await user.type(screen.getByLabelText("auth.changePassword.oldPassword"), "OldPwd1");
    await user.type(screen.getByLabelText("auth.changePassword.newPassword"), "weak");
    await user.type(screen.getByLabelText("auth.changePassword.confirmPassword"), "weak");
    await user.click(screen.getByRole("button", { name: "auth.changePassword.title" }));
    await waitFor(() => {
      expect(screen.getByText("auth.changePassword.weak")).toBeTruthy();
    });
  });
});
