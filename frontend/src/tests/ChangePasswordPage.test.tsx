import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import ChangePasswordPage from "../pages/ChangePasswordPage";
import { useAuthStore } from "../stores/authStore";

vi.mock("../api/auth");
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

describe("ChangePasswordPage", () => {
  beforeEach(() => {
    useAuthStore.setState({ token: "t" });
  });

  it("不匹配显示 mismatch", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><ChangePasswordPage /></MemoryRouter>);
    await user.type(screen.getByLabelText("当前密码"), "OldPwd1");
    await user.type(screen.getByLabelText("新密码"), "NewPwd1234");
    await user.type(screen.getByLabelText("确认新密码"), "Different1234");
    await user.click(screen.getByRole("button", { name: "修改密码" }));
    await waitFor(() => {
      expect(screen.getByText("两次输入不一致")).toBeTruthy();
    });
  });

  it("弱密码显示 weak", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><ChangePasswordPage /></MemoryRouter>);
    await user.type(screen.getByLabelText("当前密码"), "OldPwd1");
    await user.type(screen.getByLabelText("新密码"), "weak");
    await user.type(screen.getByLabelText("确认新密码"), "weak");
    await user.click(screen.getByRole("button", { name: "修改密码" }));
    await waitFor(() => {
      expect(screen.getByText("密码至少 8 位且必须包含字母和数字")).toBeTruthy();
    });
  });
});
