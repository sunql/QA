import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ProfilePage from "../pages/ProfilePage";
import { useAuthStore } from "../stores/authStore";

// react-i18next 全局 mock 在 setup.ts

describe("ProfilePage", () => {
  beforeEach(() => {
    useAuthStore.setState({
      user: {
        id: 1, username: "admin", displayName: "Admin", email: "a@x", enabled: true,
        mustChangePassword: false, roles: ["admin"], organizations: ["root"],
        tenantId: "default", lastLoginAt: null,
      } as any,
    });
  });

  it("渲染 user 字段", () => {
    render(<MemoryRouter><ProfilePage /></MemoryRouter>);
    // "admin" appears twice: username + role tag; use getAllByText
    const allAdmin = screen.getAllByText("admin");
    expect(allAdmin.length).toBeGreaterThan(0);
    expect(screen.getByText("Admin")).toBeTruthy();
  });
});
