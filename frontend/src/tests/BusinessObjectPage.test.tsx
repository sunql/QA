import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../i18n";

vi.mock("../api/businessObject", () => ({
  listBusinessObjects: vi.fn().mockResolvedValue([]),
  createBusinessObject: vi.fn(),
  updateBusinessObject: vi.fn(),
  deleteBusinessObject: vi.fn(),
}));

import BusinessObjectPage from "../pages/BusinessObjectPage";

const wrapper = ({ children }: { children: React.ReactNode }) => (
  <ConfigProvider locale={zhCN}>
    <I18nextProvider i18n={i18n}>{children}</I18nextProvider>
  </ConfigProvider>
);

describe("BusinessObjectPage", () => {
  it("renders title and new button", async () => {
    render(<BusinessObjectPage />, { wrapper });
    await waitFor(() => {
      expect(screen.getByText("业务对象")).toBeInTheDocument();
    });
  });

  it("loads and renders business objects", async () => {
    const { listBusinessObjects } = await import("../api/businessObject");
    vi.mocked(listBusinessObjects).mockResolvedValue([
      {
        code: "SUPPLIER",
        name: "供应商",
        graphLabel: "Supplier",
        headerClassId: null,
        description: null,
        createdTime: "2026-09-04T00:00:00Z",
        updatedTime: "2026-09-04T00:00:00Z",
      },
    ]);
    render(<BusinessObjectPage />, { wrapper });
    await waitFor(() => {
      expect(screen.getByText("供应商")).toBeInTheDocument();
    });
  });
});
