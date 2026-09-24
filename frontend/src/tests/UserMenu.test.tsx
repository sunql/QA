import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import UserMenu from "../components/common/UserMenu";
import { useAuthStore } from "../stores/authStore";

// react-i18next 全局 mock 在 setup.ts（vi.mock hoisted 会覆盖本地 mock，故不在此处重复）

describe("UserMenu", () => {
  beforeEach(() => {
    useAuthStore.setState({ token: "t", user: { username: "alice", displayName: "Alice" } as any, mustChangePassword: false });
  });

  it("mustChangePassword=true 时改密项有红点", async () => {
    useAuthStore.setState({ mustChangePassword: true });
    render(<MemoryRouter><UserMenu /></MemoryRouter>);
    await userEvent.click(screen.getByText("alice"));
    // i18n mock 返回 key：userMenu.changePassword（中文应在 i18n 资源里校验，不在组件测试里）
    expect(screen.getByText("userMenu.changePassword")).toBeTruthy();
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
