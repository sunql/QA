/** 本体发现工作台：三段式 Tab（发现源 / 运行 / 评审）。 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
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
vi.mock("../api/ontologyTargets", () => ({
  listOntologyClasses: vi.fn().mockResolvedValue([
    { id: 1, className: "Supplier", classAlias: "供应商", sourceTable: "DIM_SUPPLIER", objectType: "Master" },
  ]),
  listOntologyProperties: vi.fn().mockResolvedValue([
    { id: 11, classId: 1, propertyName: "supplier_name", propertyAlias: "名称" },
  ]),
  listOntologyMetrics: vi.fn().mockResolvedValue([
    { id: 21, metricName: "supplier_count", metricAlias: "供应商数" },
  ]),
}));


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
    <MemoryRouter>
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <OntologyDiscoveryPage />
      </ConfigProvider>
    </I18nextProvider>
    </MemoryRouter>,
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


function modalAt(index = 0): HTMLElement {
  return document.querySelectorAll(".ant-modal")[index] as HTMLElement;
}

async function pickComboboxIn(modal: HTMLElement, selectIndex: number, optionText: RegExp) {
  const selectors = modal.querySelectorAll(".ant-select-selector");
  fireEvent.mouseDown(selectors[selectIndex] as Element);
  const options = await screen.findAllByText(optionText);
  fireEvent.click(options[options.length - 1]);
}

it("监测变化调用后端并提示无变化", async () => {
  adminApi.monitorSource.mockResolvedValue({
    sourceId: 1, snapshotId: 1, changed: false, runId: null,
  });
  renderPage();

  await waitFor(() => expect(screen.getByText("probe:THBI Oracle")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: /监测变化/ }));

  await waitFor(() => expect(adminApi.monitorSource).toHaveBeenCalledWith(1));
});

it("登记发现源：填表提交调用 createSource", async () => {
  adminApi.createSource.mockResolvedValue({ ...SOURCE, id: 2, name: "新源" });
  renderPage();

  await waitFor(() => expect(screen.getByText("probe:THBI Oracle")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: /登记发现源/ }));

  const modal = modalAt(0);
  const inputs = modal.querySelectorAll("input");
  fireEvent.change(inputs[0] as Element, { target: { value: "新源" } });
  fireEvent.change(inputs[2] as Element, { target: { value: "张三" } });
  await pickComboboxIn(modal, 0, /THBI Oracle/);
  fireEvent.click(screen.getByRole("button", { name: /确\s*定/ }));

  await waitFor(() =>
    expect(adminApi.createSource).toHaveBeenCalledWith({
      name: "新源",
      datasourceId: 1,
      owner: "张三",
      accessPolicy: { mode: "READ_ONLY" },
      note: null,
    }),
  );
});

it("运行页签：选源后加载快照表并提交运行", async () => {
  adminApi.listSnapshots.mockResolvedValue({
    rows: [{ id: 9, sourceId: 1, schemaHash: "a".repeat(64), tableCount: 1, columnCount: 2, capturedAt: "2026-11-22T10:00:00Z", createdBy: "x" }],
    total: 1,
  });
  adminApi.getSnapshot.mockResolvedValue({
    id: 9, sourceId: 1, schemaHash: "a".repeat(64), tableCount: 1, columnCount: 2,
    capturedAt: "2026-11-22T10:00:00Z", createdBy: "x",
    snapshot: { tables: [{ tableName: "DIM_SUPPLIER", owner: "THBI", columns: [] }] },
  });
  adminApi.startRun.mockResolvedValue({ id: 12, status: "RUNNING", snapshotId: 9 });
  renderPage();

  await waitFor(() => expect(screen.getByText("probe:THBI Oracle")).toBeTruthy());
  fireEvent.click(screen.getByRole("tab", { name: "运行" }));
  fireEvent.click(await screen.findByRole("button", { name: /触发运行/ }));

  const modal = modalAt(0);
  await pickComboboxIn(modal, 0, /probe:THBI Oracle/);
  await waitFor(() => expect(adminApi.listSnapshots).toHaveBeenCalledWith({ sourceId: 1, limit: 1 }));
  await waitFor(() => expect(adminApi.getSnapshot).toHaveBeenCalledWith(9));

  fireEvent.click(screen.getByRole("button", { name: /确\s*定/ }));
  await waitFor(() =>
    expect(adminApi.startRun).toHaveBeenCalledWith({
      sourceId: 1,
      snapshotId: null,
      tables: null,
      withCandidates: true,
    }),
  );
});


it("运行页签：不可剖析的表（回收站对象）不显示，并提示已隐藏张数", async () => {
  // 回归来历（2026-11-27，用户实测）：Oracle 回收站对象
  // `BIN$…==$0` 必然被剖析护栏拒拼 ⇒ 选到它就废掉整轮运行。
  // 前端按后端给的清单**不显示**它们，并明确告诉用户「隐藏了几张」。
  adminApi.listSnapshots.mockResolvedValue({
    rows: [{ id: 9, sourceId: 1, schemaHash: "a".repeat(64), tableCount: 2, columnCount: 2, capturedAt: "2026-11-22T10:00:00Z", createdBy: "x" }],
    total: 1,
  });
  adminApi.getSnapshot.mockResolvedValue({
    id: 9, sourceId: 1, schemaHash: "a".repeat(64), tableCount: 2, columnCount: 2,
    capturedAt: "2026-11-22T10:00:00Z", createdBy: "x",
    snapshot: {
      tables: [
        { tableName: "DIM_SUPPLIER", owner: "THBI", columns: [] },
        { tableName: "BIN$W81vJmasuczgY0bNqMBvog==$0", owner: "THBI", columns: [] },
      ],
    },
    unprofilableTables: ["BIN$W81vJmasuczgY0bNqMBvog==$0"],
  });
  adminApi.startRun.mockResolvedValue({ id: 12, status: "RUNNING", snapshotId: 9 });
  renderPage();

  await waitFor(() => expect(screen.getByText("probe:THBI Oracle")).toBeTruthy());
  fireEvent.click(screen.getByRole("tab", { name: "运行" }));
  fireEvent.click(await screen.findByRole("button", { name: /触发运行/ }));

  const modal = modalAt(0);
  await pickComboboxIn(modal, 0, /probe:THBI Oracle/);
  await waitFor(() => expect(adminApi.getSnapshot).toHaveBeenCalledWith(9));

  // 提示：已隐藏 1 张不可剖析的表
  await waitFor(() =>
    expect(screen.getByText(/已隐藏 1 张不可剖析的表/)).toBeTruthy(),
  );

  // 展开表选择器 ⇒ 回收站表**不在选项里**，正常表在
  const selectors = modal.querySelectorAll(".ant-select-selector");
  fireEvent.mouseDown(selectors[1] as Element);
  await waitFor(() => expect(screen.getAllByText("THBI.DIM_SUPPLIER").length).toBeGreaterThan(0));
  expect(screen.queryByText(/BIN\$/)).toBeNull();
});


it("编辑策略：保存表白名单调用 updateSource", async () => {
  adminApi.updateSource.mockResolvedValue({ ...SOURCE });
  renderPage();

  await waitFor(() => expect(screen.getByText("probe:THBI Oracle")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: /编辑策略/ }));

  const modal = modalAt(0);
  const inputs = modal.querySelectorAll("input");
  fireEvent.change(inputs[1] as Element, { target: { value: "DIM_SUPPLIER, dim_imaterial" } });
  fireEvent.click(screen.getByRole("button", { name: /确\s*定/ }));

  await waitFor(() =>
    expect(adminApi.updateSource).toHaveBeenCalledWith(1, {
      accessPolicy: { mode: "READ_ONLY", tables: ["DIM_SUPPLIER", "dim_imaterial"] },
    }),
  );
});

it("停用发现源：调用 updateSource(DISABLED)", async () => {
  adminApi.updateSource.mockResolvedValue({ ...SOURCE, status: "DISABLED" });
  renderPage();

  await waitFor(() => expect(screen.getByText("probe:THBI Oracle")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: /停\s*用/ }));

  await waitFor(() =>
    expect(adminApi.updateSource).toHaveBeenCalledWith(1, { status: "DISABLED" }),
  );
});


it("有 RUNNING 运行时每 5 秒自动刷新", async () => {
  reviewApi.listDiscoveryRuns.mockResolvedValue({
    rows: [{ id: 7, sourceId: 1, snapshotId: 1, trigger: "MANUAL", status: "RUNNING",
             modelVersion: null, promptVersion: null, promptTokens: 0, completionTokens: 0,
             cost: 0, errorMessage: null, startedAt: "2026-11-22T10:00:00Z", finishedAt: null,
             createdBy: "x" }],
    total: 1,
  });
  renderPage();
  fireEvent.click(await screen.findByRole("tab", { name: "运行" }));
  await waitFor(() => expect(reviewApi.listDiscoveryRuns).toHaveBeenCalled());
  const calls = reviewApi.listDiscoveryRuns.mock.calls.length;

  await new Promise((resolve) => setTimeout(resolve, 5200));

  expect(reviewApi.listDiscoveryRuns.mock.calls.length).toBeGreaterThan(calls);
}, 15000);
