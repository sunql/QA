/** M4-2 绑定向导页测试：三态列表 + 冻结点警示 + D-3 候选态语义（mock API）。 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { describe, expect, it, vi } from "vitest";
import i18n from "../../i18n/i18n";
import ProcurementBindingsPage from "../ProcurementBindingsPage";
import type { SourceBinding } from "../../api/procurementBindings";

const api = vi.hoisted(() => ({
  listSourceBindings: vi.fn(),
  getSourceBinding: vi.fn(),
  createSourceBinding: vi.fn(),
  createPropertyBinding: vi.fn(),
  confirmSourceBinding: vi.fn(),
  confirmPropertyBinding: vi.fn(),
  expireSourceBinding: vi.fn(),
  listClasses: vi.fn(),
  listDataSources: vi.fn(),
  listPropertiesByClass: vi.fn(),
}));

vi.mock("../../api/procurementBindings", () => api);
vi.mock("../../api/ontology", () => ({
  listClasses: api.listClasses,
  listPropertiesByClass: api.listPropertiesByClass,
}));
vi.mock("../../api/datasource", () => ({
  listDataSources: api.listDataSources,
}));

function makeBinding(overrides: Partial<SourceBinding> = {}): SourceBinding {
  return {
    id: 1,
    classId: 3,
    className: "DIM_SUPPLIER",
    datasourceId: 1,
    schemaName: "THBI",
    sourceTable: "DIM_SUPPLIER",
    bindingRole: "BASE",
    filterExpression: null,
    filterAllowlist: [],
    isCandidate: true,
    confirmedBy: null,
    confirmedTime: null,
    validFrom: "2026-11-20T00:00:00Z",
    validTo: null,
    createdBy: "m1-1-test",
    propertyCount: 2,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <ProcurementBindingsPage />
      </ConfigProvider>
    </I18nextProvider>,
  );
}


describe("ProcurementBindingsPage", () => {
  it("渲染三态标签（候选/已确认/已失效）", async () => {
    api.listSourceBindings.mockResolvedValue([
      makeBinding({ id: 1, isCandidate: true }),
      makeBinding({ id: 2, isCandidate: false, confirmedBy: "admin" }),
      makeBinding({ id: 3, isCandidate: false, validTo: "2026-11-21T00:00:00Z" }),
    ]);
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("候选")).toBeTruthy();
      expect(screen.getByText("已确认")).toBeTruthy();
      expect(screen.getByText("已失效")).toBeTruthy();
    });
  });

  it("D-3 语义：候选绑定带警示（不被读路径消费）", async () => {
    api.listSourceBindings.mockResolvedValue([makeBinding()]);
    api.getSourceBinding.mockResolvedValue({
      binding: makeBinding(),
      properties: [
        {
          id: 11,
          sourceBindingId: 1,
          propertyId: 1,
          propertyName: "SUPPLIER_CODE",
          sourceColumn: "SUPPLIER_CODE",
          keyOrdinal: 1,
          transformExpression: null,
          isCandidate: true,
          confirmedBy: null,
          confirmedTime: null,
        },
      ],
    });
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("候选")).toBeTruthy();
    });
    fireEvent.click(
      screen.getAllByRole("button", { name: /详\s*情/ })[0],
    );
    // 详情打开 ⇒ 候选属性未全确认 ⇒ 「先确认全部属性」警示（fail-closed 提示）
    await waitFor(
      () => {
        expect(
          screen.getAllByText(/源绑定确认前必须全部确认/).length,
        ).toBeGreaterThan(0);
      },
      { timeout: 4000 },
    );
    // 候选态 ⇒ 确认按钮存在；点开 Popconfirm ⇒ 冻结点警示（版本冻结点）
    fireEvent.click(screen.getByText("确认绑定"));
    await waitFor(() => {
      expect(screen.getAllByText(/版本冻结点/).length).toBeGreaterThan(0);
    });
  });

  it("已确认且失效的绑定不出现确认按钮（终态）", async () => {
    api.listSourceBindings.mockResolvedValue([
      makeBinding({ id: 2, isCandidate: false, confirmedBy: "admin" }),
      makeBinding({ id: 3, isCandidate: false, validTo: "2026-11-21T00:00:00Z" }),
    ]);
    api.getSourceBinding.mockResolvedValue({
      binding: makeBinding({ id: 2, isCandidate: false, confirmedBy: "admin" }),
      properties: [
        {
          id: 11, sourceBindingId: 2, propertyId: 1, propertyName: "SUPPLIER_CODE",
          sourceColumn: "SUPPLIER_CODE", keyOrdinal: 1, transformExpression: null,
          isCandidate: false, confirmedBy: "admin", confirmedTime: "2026-11-21T00:00:00Z",
        },
      ],
    });
    renderPage();
    await waitFor(() => {
      expect(screen.getByText("已确认")).toBeTruthy();
    });
    fireEvent.click(screen.getAllByRole("button", { name: /详\s*情/ })[0]);
    await waitFor(
      () => {
        expect(screen.getAllByText("SUPPLIER_CODE").length).toBeGreaterThan(0);
      },
      { timeout: 4000 },
    );
    expect(screen.queryByText("确认绑定")).toBeNull();
  });
});
