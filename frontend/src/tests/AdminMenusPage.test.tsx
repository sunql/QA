/** AdminMenusPage 编辑表单 labelKey 预填（RED）。
 *
 * 需求：点击某行「编辑」按钮后，表单的 labelKey 输入框应展示列表视图同列看到的
 * 已解析文案（即 `t(labelKey)` 的返回值），而不是原始 i18n key。
 * 原因：原始 key 写回数据库后，再次打开编辑会显示混乱的字符串；用户期望所见即所编辑。
 *
 * RED：本测试在原始代码（labelKey: rec.labelKey）下应该断言失败。
 * GREEN：把 onEdit 里 labelKey 改成 `labelMap.get(rec.code) ?? rec.labelKey` 后通过。
 */
import { describe, expect, it, vi, beforeEach, afterAll } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../i18n";

// Console 转发：在 jsdom 下 antd 静默抛错时常被 catch，需要看 console.error。
const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {});

const { sectionRow, leafRow } = vi.hoisted(() => ({
    sectionRow: {
        id: 100,
        code: "section.systemConfig",
        parentId: null,
        parentCode: null,
        labelKey: "menu.section.systemConfig", // zh-CN 翻译：系统信息配置
        iconCode: "api",
        sortOrder: 500,
        path: null,
        permissionCode: null,
        visible: true,
        hasChildren: false,
    },
    leafRow: {
        id: 101,
        code: "item.adminUsers",
        parentId: 100,
        parentCode: "section.systemConfig",
        labelKey: "menu.item.adminUsers", // zh-CN 翻译：用户管理
        iconCode: "user",
        sortOrder: 540,
        path: "/admin/users",
        permissionCode: null,
        visible: true,
        hasChildren: false,
    },
}));

vi.mock("../api/menuConfig", () => ({
    listMenuRows: vi.fn().mockResolvedValue([sectionRow, leafRow]),
    listMenuTree: vi.fn().mockResolvedValue([]),
    createMenu: vi.fn(),
    updateMenu: vi.fn(),
    deleteMenu: vi.fn(),
}));

import AdminMenusPage from "../pages/AdminMenusPage";

describe("AdminMenusPage 编辑表单 labelKey 预填", () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    afterAll(() => {
        consoleSpy.mockRestore();
    });

    it("点击一级类行「编辑」后，labelKey 输入框展示列表显示文案（zh-CN 翻译）", async () => {
        render(
            <ConfigProvider locale={zhCN}>
                <I18nextProvider i18n={i18n}>
                    <AdminMenusPage />
                </I18nextProvider>
            </ConfigProvider>,
        );

        // 切到「列表视图」tab（默认是 tree，tree 是空数据，看不到行）。
        await waitFor(() => {
            expect(
                screen.getByRole("tab", { name: /列表视图|list/i }),
            ).toBeInTheDocument();
        });
        fireEvent.click(screen.getByRole("tab", { name: /列表视图|list/i }));

        // 列表视图应展示翻译后的 label（系统信息配置）。
        await waitFor(
            () => {
                // 可能会有多个匹配（如 Modal 内未销毁）；用 getAllByText 至少一个即可
                expect(
                    screen.getAllByText("系统信息配置").length,
                ).toBeGreaterThan(0);
            },
            { timeout: 3000 },
        );

        // 列表里能看到 section.systemConfig 行（按 code 兜底显示）。
        await waitFor(() => {
            expect(screen.getByText("section.systemConfig")).toBeInTheDocument();
        });

        // 找出该行对应的编辑按钮：同一行 Space 内，按钮紧邻 code 列。
        // antd 中文 Button 字符间距：文本为「编 辑」（中间空格），不能用 /编辑/。
        const editButtons = screen.getAllByRole("button", { name: /编\s*辑/i });
        expect(editButtons.length).toBeGreaterThan(0);
        fireEvent.click(editButtons[0]);

        // 关键断言：编辑表单的 labelKey 输入框应展示列表视图显示文案，
        // 而不是原始 i18n key「menu.section.systemConfig」。
        await waitFor(() => {
            const input = screen.getByDisplayValue("系统信息配置");
            expect(input).toBeInTheDocument();
        });

        // 反向断言：原始 i18n key 不应作为 labelKey 的输入值出现。
        expect(screen.queryByDisplayValue("menu.section.systemConfig")).toBeNull();
    });

    it("点击叶子项行「编辑」后，labelKey 输入框同样展示翻译后的中文文案", async () => {
        render(
            <ConfigProvider locale={zhCN}>
                <I18nextProvider i18n={i18n}>
                    <AdminMenusPage />
                </I18nextProvider>
            </ConfigProvider>,
        );

        await waitFor(() => {
            expect(
                screen.getByRole("tab", { name: /列表视图|list/i }),
            ).toBeInTheDocument();
        });
        fireEvent.click(screen.getByRole("tab", { name: /列表视图|list/i }));

        await waitFor(
            () => {
                expect(screen.getByText("用户管理")).toBeInTheDocument();
            },
            { timeout: 3000 },
        );

        await waitFor(() => {
            expect(screen.getByText("item.adminUsers")).toBeInTheDocument();
        });

        const editButtons = screen.getAllByRole("button", { name: /编\s*辑/i });
        expect(editButtons.length).toBeGreaterThan(0);
        // 编辑按钮顺序：displayRows = sections 优先（section.systemConfig），
        // 然后按 parent 分组放叶子。所以第 2 个编辑按钮 = item.adminUsers。
        fireEvent.click(editButtons[1]);

        await waitFor(() => {
            const input = screen.getByDisplayValue("用户管理");
            expect(input).toBeInTheDocument();
        });

        expect(screen.queryByDisplayValue("menu.item.adminUsers")).toBeNull();
    });
});