import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation, Route, Routes } from "react-router-dom";
import RequireAuth from "../RequireAuth";
import { useAuthStore } from "../../../stores/authStore";
import type { AuthMeRead } from "../../../types/auth";

// fix/login-401-white-screen（2026-10-25）：mustChangePassword 重定向目标与
// 白名单统一改为 App.tsx 真实路由 /change-password（旧 /auth/change-password
// 是不存在的路径 → 白屏）。

const mockUser: AuthMeRead = {
  id: 1,
  username: "test",
  displayName: "Test",
  email: null,
  enabled: true,
  mustChangePassword: false,
  roles: [],
  organizations: [],
  tenantId: "default",
  lastLoginAt: null,
};

const mockUserMustChange: AuthMeRead = {
  ...mockUser,
  mustChangePassword: true,
};

// 哨兵组件：渲染到 <RequireAuth /> 之外（与 RequireAuth 并列），
// 放在 MemoryRouter 内即可观察到 URL 变化
function LocationProbe(): JSX.Element {
  const { pathname } = useLocation();
  return <div data-testid="probe-path">{pathname}</div>;
}

// 测试用子路由
function TestOutlet(): JSX.Element {
  return <div>child content</div>;
}

describe("RequireAuth", () => {
  beforeEach(() => {
    useAuthStore.setState({ token: null, user: null, mustChangePassword: false });
  });

  it("redirects to /login when no token", () => {
    render(
      <MemoryRouter initialEntries={["/some-page"]}>
        <Routes>
          <Route element={<RequireAuth />}>
            <Route path="/some-page" element={<TestOutlet />} />
          </Route>
          <Route path="/login" element={<div>login page</div>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    );
    expect(screen.getByTestId("probe-path").textContent).toBe("/login");
  });

  it("does NOT redirect when mustChangePassword=false on a business page", () => {
    useAuthStore.setState({ token: "valid-token", user: mockUser, mustChangePassword: false });
    render(
      <MemoryRouter initialEntries={["/some-business-page"]}>
        <Routes>
          <Route element={<RequireAuth />}>
            <Route path="/some-business-page" element={<TestOutlet />} />
          </Route>
          <Route path="/change-password" element={<div>change-password page</div>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    );
    expect(screen.getByTestId("probe-path").textContent).toBe("/some-business-page");
  });

  it("redirects to /change-password when mustChangePassword=true on a non-allowed path", () => {
    useAuthStore.setState({ token: "valid-token", user: mockUserMustChange, mustChangePassword: true });
    render(
      <MemoryRouter initialEntries={["/some-business-page"]}>
        <Routes>
          <Route element={<RequireAuth />}>
            <Route path="/some-business-page" element={<TestOutlet />} />
          </Route>
          <Route path="/change-password" element={<div>change-password page</div>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    );
    expect(screen.getByTestId("probe-path").textContent).toBe("/change-password");
  });

  it("does NOT redirect when mustChangePassword=true but path is /change-password", () => {
    useAuthStore.setState({ token: "valid-token", user: mockUserMustChange, mustChangePassword: true });
    render(
      <MemoryRouter initialEntries={["/change-password"]}>
        <Routes>
          <Route element={<RequireAuth />}>
            <Route path="/change-password" element={<TestOutlet />} />
          </Route>
          <Route path="/login" element={<div>login page</div>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    );
    expect(screen.getByTestId("probe-path").textContent).toBe("/change-password");
  });

  it("redirects to /login when token=null even on /change-password", () => {
    useAuthStore.setState({ token: null, user: null, mustChangePassword: false });
    render(
      <MemoryRouter initialEntries={["/change-password"]}>
        <Routes>
          <Route element={<RequireAuth />}>
            <Route path="/change-password" element={<TestOutlet />} />
          </Route>
          <Route path="/login" element={<div>login page</div>} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    );
    expect(screen.getByTestId("probe-path").textContent).toBe("/login");
  });
});
