import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { I18nextProvider } from "react-i18next";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";

import AdminSystemConfigPage from "../pages/AdminSystemConfigPage";

vi.mock("../api/systemConfig", () => ({
    listSystemConfig: vi.fn().mockResolvedValue([
        {
            key: "ENABLE_L4_AGENT_LOOP",
            value: "false",
            description: "L4 LangGraph agent loop switch",
            updatedTime: "2026-09-11T00:00:00Z",
        },
        {
            key: "ANOTHER_KEY",
            value: null,
            description: "An example with null value",
            updatedTime: null,
        },
    ]),
    updateSystemConfig: vi.fn().mockResolvedValue({
        key: "ENABLE_L4_AGENT_LOOP",
        value: "true",
        description: "L4 LangGraph agent loop switch",
        updatedTime: "2026-09-11T01:00:00Z",
    }),
    getSystemConfig: vi.fn(),
}));

// 极简 i18n init（仅本页用到的 keys）
i18next.use(initReactI18next).init({
    lng: "zh-CN",
    fallbackLng: "zh-CN",
    ns: ["translation"],
    defaultNS: "translation",
    resources: {
        "zh-CN": {
            translation: {
                "systemConfig.title": "系统参数",
                "systemConfig.columns.key": "参数名",
                "systemConfig.columns.value": "值",
                "systemConfig.columns.description": "说明",
                "systemConfig.columns.updatedTime": "更新时间",
                "systemConfig.columns.actions": "操作",
                "systemConfig.actions.edit": "编辑",
                "systemConfig.actions.refresh": "刷新",
                "systemConfig.actions.save": "保存",
                "systemConfig.actions.cancel": "取消",
                "systemConfig.modal.editTitle": "编辑系统参数",
                "systemConfig.modal.keyReadonlyHelp":
                    "key 为不可变主键，需新增请走 alembic 迁移 + 种子脚本",
                "systemConfig.modal.valuePlaceholder":
                    "支持空串（清空）、布尔字符串、JSON-like 文本",
                "systemConfig.messages.updated": "更新成功",
                "systemConfig.errors.loadFailed": "加载失败",
                "systemConfig.errors.updateFailed": "更新失败",
                "systemConfig.errors.valueTooLong": "value 最长 4096 字符",
                "systemConfig.empty": "空",
            },
        },
    },
    interpolation: { escapeValue: false },
});

const renderWithProviders = (component: React.ReactNode) => {
    return render(
        <I18nextProvider i18n={i18next}>
            <ConfigProvider>{component}</ConfigProvider>
        </I18nextProvider>,
    );
};

describe("AdminSystemConfigPage", () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it("renders rows from listSystemConfig API", async () => {
        renderWithProviders(<AdminSystemConfigPage />);
        await screen.findAllByText("ENABLE_L4_AGENT_LOOP");
        // 第二个 key
        expect(screen.getByText("ANOTHER_KEY")).toBeTruthy();
    });

    it("renders null value as '空' marker", async () => {
        renderWithProviders(<AdminSystemConfigPage />);
        await screen.findAllByText("ANOTHER_KEY");
        // 空值显示 "（空）"
        expect(screen.getAllByText(/空/).length).toBeGreaterThan(0);
    });

    it("exposes refresh button in toolbar", async () => {
        renderWithProviders(<AdminSystemConfigPage />);
        await screen.findAllByText("ENABLE_L4_AGENT_LOOP");
        const buttons = screen
            .getAllByRole("button")
            .map((b) => (b.textContent || "").replace(/\s+/g, ""));
        expect(buttons).toContain("刷新");
    });

    it("exposes edit button per row", async () => {
        renderWithProviders(<AdminSystemConfigPage />);
        await screen.findAllByText("ENABLE_L4_AGENT_LOOP");
        const buttons = screen
            .getAllByRole("button")
            .map((b) => (b.textContent || "").replace(/\s+/g, ""));
        // 2 行 × 1 edit = 2 个编辑按钮
        expect(buttons.filter((b) => b === "编辑").length).toBe(2);
    });

    it("opens modal with current value on edit click", async () => {
        renderWithProviders(<AdminSystemConfigPage />);
        await screen.findAllByText("ENABLE_L4_AGENT_LOOP");
        const editButtons = screen
            .getAllByRole("button")
            .filter((b) => (b.textContent || "").replace(/\s+/g, "") === "编辑");
        fireEvent.click(editButtons[0]);
        await screen.findByText(/编辑系统参数/);
    });

    it("calls updateSystemConfig with new value", async () => {
        const { updateSystemConfig } = await import("../api/systemConfig");
        renderWithProviders(<AdminSystemConfigPage />);
        await screen.findAllByText("ENABLE_L4_AGENT_LOOP");

        // 点编辑打开 modal
        const editButtons = screen
            .getAllByRole("button")
            .filter((b) => (b.textContent || "").replace(/\s+/g, "") === "编辑");
        fireEvent.click(editButtons[0]);
        await screen.findByText(/编辑系统参数/);

        // 修改 value
        const textareas = document.querySelectorAll("textarea");
        // 第 2 个 textarea 是 value（第一个是 description 禁用，第二个是 value 可编辑）
        const valueTextarea = textareas[textareas.length - 1] as HTMLTextAreaElement;
        fireEvent.change(valueTextarea, { target: { value: "true" } });

        // 点保存（ok button）
        const okButton = screen
            .getAllByRole("button")
            .find((b) => (b.textContent || "").replace(/\s+/g, "") === "保存");
        expect(okButton).toBeTruthy();
        fireEvent.click(okButton!);

        await waitFor(() => {
            expect(updateSystemConfig).toHaveBeenCalledWith(
                "ENABLE_L4_AGENT_LOOP",
                { value: "true" },
            );
        });
    });

    it("shows error message on load failure", async () => {
        const { listSystemConfig } = await import("../api/systemConfig");
        (listSystemConfig as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
            new Error("boom"),
        );
        renderWithProviders(<AdminSystemConfigPage />);
        // mock 拒绝 → 列表为空 → 等待 ant-message-error 元素出现
        await waitFor(() => {
            expect(
                document.querySelector(".ant-message-error"),
            ).toBeTruthy();
        });
    });
});