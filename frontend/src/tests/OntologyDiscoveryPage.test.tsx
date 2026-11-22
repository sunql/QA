/** 本体发现工作台：三段式 Tab（发现源 / 运行 / 评审）。 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { describe, expect, it, vi, beforeEach } from "vitest";
import i18n from "../i18n/i18n";
import OntologyDiscoveryPage from "../pages/OntologyDiscoveryPage";
import { useAuthStore } from "../stores/authStore";

const adminApi = vi.hoisted(() => ({
  listSources: vi.fn(),
  listDatasources: vi.fn(),
  createSource: vi.fn(),
  updateSource: vi.fn(),
  captureSnapshot: vi.fn(),
  monitorSource: vi.fn(),
  listSnapshots: vi.fn(),
  getSnapshot: vi.fn(),
  startRun: vi.fn(),
}));
vi.mock("../api/discoveryAdmin", () => adminApi);

const reviewApi = vi.hoisted(() => ({
  listDiscoveryRuns: vi.fn(),
  listCandidates: vi.fn(),
  listFindings: vi.fn(),
  decideCandidate: vi.fn(),
  decideFinding: vi.fn(),
}));
vi.mock("../api/discoveryReview", () => reviewApi);

const SOURCE = {
  id: 1,
  name: "probe:THBI Oracle",
  datasourceId: 1,
  owner: "probe",
  accessPolicy: { mode: "READ_ONLY" },
  status: "ACTIVE",
  note: null,
  createdBy: "probe",
  createdTime: "2026-11-22T09:00:00Z",
  snapshotCount: 1,
};

function renderPage() {
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <OntologyDiscoveryPage />
      </ConfigProvider>
    </I18nextProvider>,
  );
}

describe("OntologyDiscoveryPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ user: { username: "admin", roles: ["admin"] } as never });
    adminApi.listSources.mockResolvedValue({ rows: [SOURCE], total: 1 });
    adminApi.listDatasources.mockResolvedValue([{ id: 1, name: "THBI Oracle", type: "oracle", isActive: true }]);
    adminApi.listSnapshots.mockResolvedValue({ rows: [], total: 0 });
    reviewApi.listDiscoveryRuns.mockResolvedValue({ rows: [], total: 0 });
    reviewApi.listCandidates.mockResolvedValue({ rows: [], total: 0 });
    reviewApi.listFindings.mockResolvedValue({ rows: [], total: 0 });
  });

  it("默认展示发现源（含业务数据源名）", async () => {
    renderPage();

    await waitFor(() => expect(screen.getByText("probe:THBI Oracle")).toBeTruthy());
    expect(screen.getByText("THBI Oracle")).toBeTruthy();
    expect(screen.getByRole("button", { name: /登记发现源/ })).toBeTruthy();
  });

  it("切到运行页签有触发运行入口", async () => {
    renderPage();

    await waitFor(() => expect(screen.getByText("发现源")).toBeTruthy());
    fireEvent.click(screen.getByRole("tab", { name: "运行" }));

    await waitFor(() => expect(screen.getByRole("button", { name: /触发运行/ })).toBeTruthy());
    expect(reviewApi.listDiscoveryRuns).toHaveBeenCalled();
  });

  it("采集快照调用后端并提示复用状态", async () => {
    adminApi.captureSnapshot.mockResolvedValue({
      snapshotId: 1,
      sourceId: 1,
      schemaHash: "a".repeat(64),
      tableCount: 2,
      columnCount: 478,
      capturedAt: "2026-11-22T09:00:00Z",
      reused: true,
    });
    renderPage();

    await waitFor(() => expect(screen.getByText("probe:THBI Oracle")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /采集快照/ }));

    await waitFor(() => expect(adminApi.captureSnapshot).toHaveBeenCalledWith(1));
  });
});
