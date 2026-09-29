import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { i18n } from "../i18n";

vi.mock("../api/users", () => ({
    listUsers: vi.fn(),
    createUser: vi.fn(),
    updateUser: vi.fn(),
    deleteUser: vi.fn(),
    setUserRoles: vi.fn(),
    setUserOrganizations: vi.fn(),
    setUserPermissions: vi.fn(),
    getUserEffectivePermissions: vi.fn(),
    adminResetPassword: vi.fn(),
}));
vi.mock("../api/roles", () => ({ listRoles: vi.fn().mockResolvedValue([]) }));
vi.mock("../api/organizations", () => ({
    listOrganizations: vi.fn().mockResolvedValue([]),
}));
vi.mock("../api/menuConfig", () => ({ listMenuRows: vi.fn().mockResolvedValue([]) }));

import AdminUsersPage from "../pages/AdminUsersPage";

const wrap = (children: React.ReactNode) => (
    <ConfigProvider locale={zhCN}>
        <I18nextProvider i18n={i18n}>{children}</I18nextProvider>
    </ConfigProvider>
);

const aliceRow = {
    id: 1,
    username: "alice",
    displayName: "Alice",
    email: null,
    enabled: true,
    roleIds: [],
    roleCodes: [],
    organizationIds: [],
    organizationCodes: [],
    createdTime: "2026-01-01",
    updatedTime: null,
};

describe("AdminUsersPage password fields", () => {
    beforeEach(() => vi.clearAllMocks());

    it("create modal shows required password field (中文 密码 + type=password input)", async () => {
        render(wrap(<AdminUsersPage />));
        fireEvent.click(screen.getByRole("button", { name: /新建用户/ }));
        expect(screen.getAllByText(/^密码$/).length).toBeGreaterThan(0);
        const passwordInputs = document.querySelectorAll('input[type="password"]');
        expect(passwordInputs.length).toBeGreaterThan(0);
    });

    it("submit without password does not call createUser (前端校验拒)", async () => {
        const usersApi = await import("../api/users");
        (usersApi.listUsers as ReturnType<typeof vi.fn>).mockResolvedValue([]);
        render(wrap(<AdminUsersPage />));
        fireEvent.click(screen.getByRole("button", { name: /新建用户/ }));
        fireEvent.change(screen.getByLabelText(/用户名/, { selector: "input" }), {
            target: { value: "alice" },
        });
        fireEvent.change(screen.getByLabelText(/显示名/, { selector: "input" }), {
            target: { value: "Alice" },
        });
        fireEvent.click(screen.getByRole("button", { name: /确\s*定/ }));
        await waitFor(() => {
            expect(usersApi.createUser).not.toHaveBeenCalled();
        });
    });

    it("edit with filled password triggers adminResetPassword", async () => {
        const usersApi = await import("../api/users");
        (usersApi.listUsers as ReturnType<typeof vi.fn>).mockResolvedValue([aliceRow]);
        render(wrap(<AdminUsersPage />));
        await waitFor(() => expect(screen.getByText("alice")).toBeTruthy());
        fireEvent.click(screen.getByRole("button", { name: /^编\s*辑$/ }));
        const passwordInput = document.querySelector(
            'input[type="password"]',
        ) as HTMLInputElement;
        fireEvent.change(passwordInput, { target: { value: "NewPass456" } });
        fireEvent.click(screen.getByRole("button", { name: /确\s*定/ }));
        await waitFor(() => {
            expect(usersApi.updateUser).toHaveBeenCalled();
            expect(usersApi.adminResetPassword).toHaveBeenCalledWith(
                1,
                expect.objectContaining({
                    newPassword: "NewPass456",
                    forceChangeOnNextLogin: true,
                }),
            );
        });
    });

    it("edit without password does NOT call adminResetPassword", async () => {
        const usersApi = await import("../api/users");
        (usersApi.listUsers as ReturnType<typeof vi.fn>).mockResolvedValue([aliceRow]);
        render(wrap(<AdminUsersPage />));
        await waitFor(() => expect(screen.getByText("alice")).toBeTruthy());
        fireEvent.click(screen.getByRole("button", { name: /^编\s*辑$/ }));
        fireEvent.click(screen.getByRole("button", { name: /确\s*定/ }));
        await waitFor(() => expect(usersApi.updateUser).toHaveBeenCalled());
        expect(usersApi.adminResetPassword).not.toHaveBeenCalled();
    });
});
