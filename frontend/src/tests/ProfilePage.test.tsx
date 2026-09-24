import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import ProfilePage from "../pages/ProfilePage";
import { useAuthStore } from "../stores/authStore";

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

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
