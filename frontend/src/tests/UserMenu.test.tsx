import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import UserMenu from "../components/common/UserMenu";
import { useAuthStore } from "../stores/authStore";

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

describe("UserMenu", () => {
  beforeEach(() => {
    useAuthStore.setState({ token: "t", user: { username: "alice", displayName: "Alice" } as any, mustChangePassword: false });
  });

  it("mustChangePassword=true 时改密项有红点", async () => {
    useAuthStore.setState({ mustChangePassword: true });
    render(<MemoryRouter><UserMenu /></MemoryRouter>);
    await userEvent.click(screen.getByText("alice"));
    // Dropdown renders Chinese "修改密码" (i18n mock returns key, which antd's Menu renders)
    expect(screen.getByText("修改密码")).toBeTruthy();
    // Badge.dot 渲染出 .ant-badge-dot
    expect(document.querySelector(".ant-badge-dot")).toBeTruthy();
  });

  it("无 mustChangePassword 时无红点", async () => {
    useAuthStore.setState({ mustChangePassword: false });
    render(<MemoryRouter><UserMenu /></MemoryRouter>);
    await userEvent.click(screen.getByText("alice"));
    expect(document.querySelector(".ant-badge-dot")).toBeNull();
  });
});
