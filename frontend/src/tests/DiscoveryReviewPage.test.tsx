/** G5 发现评审页：候选/问题列表 + 判定入口 + 非 admin 只读。 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi, beforeEach } from "vitest";
import i18n from "../i18n/i18n";
import DiscoveryReviewPage from "../pages/DiscoveryReviewPage";
import { useAuthStore } from "../stores/authStore";

const api = vi.hoisted(() => ({
  listDiscoveryRuns: vi.fn(),
  listCandidates: vi.fn(),
  listFindings: vi.fn(),
  decideCandidate: vi.fn(),
  decideFinding: vi.fn(),
}));
vi.mock("../api/discoveryReview", () => api);
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


const RUN = {
  id: 7,
  sourceId: 1,
  snapshotId: 1,
  trigger: "MANUAL",
  status: "SUCCEEDED",
  modelVersion: "deepseek-flash",
  promptVersion: "v1",
  promptTokens: 100,
  completionTokens: 20,
  cost: 0.01,
  errorMessage: null,
  startedAt: "2026-11-22T10:00:00Z",
  finishedAt: "2026-11-22T10:01:00Z",
  createdBy: "admin",
};

const CANDIDATE = {
  id: 11,
  runId: 7,
  sourceId: 1,
  snapshotId: 1,
  candidateType: "OBJECT",
  tableName: "DIM_SUPPLIER",
  columnName: "BPSNUM_0",
  targetCode: "SUPPLIER",
  payload: {},
  evidenceRefs: [{ ref: "DIM_SUPPLIER.BPSNUM_0", summary: "rowCount=3500" }],
  confidence: 0.95,
  modelVersion: "deepseek-flash",
  promptVersion: "v1",
  status: "CANDIDATE",
  decidedBy: null,
  decidedTime: null,
  decisionNote: null,
  createdTime: "2026-11-22T10:01:00Z",
};

const FINDING = {
  id: 3,
  runId: 7,
  sourceId: 1,
  snapshotId: 1,
  findingType: "MISSING",
  tableName: "DIM_SUPPLIER",
  columnName: "BPSNUM_0",
  severity: "WARNING",
  status: "OPEN",
  detail: "剖析未声明主键",
  evidenceRefs: [],
  decisionNote: null,
  createdTime: "2026-11-22T10:01:00Z",
};

function renderPage() {
  return render(
    <MemoryRouter>
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <DiscoveryReviewPage />
      </ConfigProvider>
    </I18nextProvider>
    </MemoryRouter>,
  );
}

describe("DiscoveryReviewPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listDiscoveryRuns.mockResolvedValue({ rows: [RUN], total: 1 });
    api.listCandidates.mockResolvedValue({ rows: [CANDIDATE], total: 1 });
    api.listFindings.mockResolvedValue({ rows: [FINDING], total: 1 });
  });

  it("展示候选与签收进度", async () => {
    useAuthStore.setState({ user: { username: "admin", roles: ["admin"] } as never });

    renderPage();

    await waitFor(() => expect(screen.getByText("SUPPLIER")).toBeTruthy());
    expect(screen.getByText("OBJECT")).toBeTruthy();
    expect(screen.getByText(/已判定\s*0\s*\/\s*2/)).toBeTruthy();
  });

  it("非 admin：判定按钮禁用", async () => {
    useAuthStore.setState({ user: { username: "u", roles: ["analyst"] } as never });

    renderPage();

    await waitFor(() => expect(screen.getByText("SUPPLIER")).toBeTruthy());
    const accept = screen.getByRole("button", { name: /采\s*纳/ });
    expect((accept as HTMLButtonElement).disabled).toBe(true);
  });

  it("admin：点采纳弹窗并提交判定", async () => {
    useAuthStore.setState({ user: { username: "admin", roles: ["admin"] } as never });
    api.decideCandidate.mockResolvedValue({ ...CANDIDATE, status: "ACCEPTED" });

    renderPage();
    await waitFor(() => expect(screen.getByText("SUPPLIER")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /采\s*纳/ }));

    await waitFor(() => expect(screen.getByPlaceholderText(/备注（驳回理由/)).toBeTruthy());
    const buttons = screen.getAllByRole("button", { name: /采\s*纳/ });
    fireEvent.click(buttons[buttons.length - 1]);

    await waitFor(() => expect(api.decideCandidate).toHaveBeenCalledWith(11, {
      status: "ACCEPTED",
      note: null,
    }));
  });
});


it("admin：关联本体弹窗可保存关联", async () => {
  api.decideCandidate.mockResolvedValue({ ...CANDIDATE, linkedEntityType: "ONTOLOGY_CLASS", linkedEntityCode: "Supplier" });
  renderPage();

  await waitFor(() => expect(screen.getByText("SUPPLIER")).toBeTruthy());
  fireEvent.click(screen.getAllByRole("button", { name: /关联本体/ })[0]);

  await waitFor(() => expect(screen.getByText(/本体里已有类 Supplier/)).toBeTruthy());
  const linkModal = Array.from(document.querySelectorAll(".ant-modal")).find(
    (node) => node.textContent?.includes("关联到本体对象"),
  ) as HTMLElement;

  const selectors = linkModal.querySelectorAll(".ant-select-selector");
  fireEvent.mouseDown(selectors[1] as Element); // 第二个下拉 = 关联对象
  const options = await screen.findAllByText(/^Supplier/);
  fireEvent.click(options[options.length - 1]);
  fireEvent.click(linkModal.querySelector(".ant-modal-footer .ant-btn-primary") as Element);

  await waitFor(() =>
    expect(api.decideCandidate).toHaveBeenLastCalledWith(11, {
      status: "CANDIDATE",
      linkedEntityType: "ONTOLOGY_CLASS",
      linkedEntityCode: "Supplier",
    }),
  );
});


it("admin：问题关联本体走 decideFinding", async () => {
  api.decideFinding.mockResolvedValue({ ...FINDING, linkedEntityType: "ONTOLOGY_METRIC", linkedEntityCode: "supplier_count" });
  renderPage();

  await waitFor(() => expect(screen.getByText("SUPPLIER")).toBeTruthy());
  fireEvent.click(screen.getByRole("tab", { name: /主数据问题/ }));
  await waitFor(() => expect(screen.getByText("MISSING")).toBeTruthy());

  fireEvent.click(screen.getAllByRole("button", { name: /关联本体/ })[0]);
  const linkModal = Array.from(document.querySelectorAll(".ant-modal")).find(
    (node) => node.textContent?.includes("关联到本体对象"),
  ) as HTMLElement;
  const selectors = linkModal.querySelectorAll(".ant-select-selector");
  fireEvent.mouseDown(selectors[0] as Element);
  fireEvent.click((await screen.findAllByText("本体指标")).pop() as Element);
  fireEvent.mouseDown(selectors[1] as Element);
  fireEvent.click((await screen.findAllByText(/supplier_count/)).pop() as Element);
  fireEvent.click(linkModal.querySelector(".ant-modal-footer .ant-btn-primary") as Element);

  await waitFor(() =>
    expect(api.decideFinding).toHaveBeenLastCalledWith(3, {
      status: "OPEN",
      linkedEntityType: "ONTOLOGY_METRIC",
      linkedEntityCode: "supplier_count",
    }),
  );
});


it("admin：问题用判定弹窗置已解决", async () => {
  api.decideFinding.mockResolvedValue({ ...FINDING, status: "RESOLVED" });
  renderPage();

  await waitFor(() => expect(screen.getByText("SUPPLIER")).toBeTruthy());
  fireEvent.click(screen.getByRole("tab", { name: /主数据问题/ }));
  await waitFor(() => expect(screen.getByText("MISSING")).toBeTruthy());

  fireEvent.click(screen.getByRole("button", { name: /已解决/ }));
  await waitFor(() => expect(screen.getByPlaceholderText(/备注（驳回理由/)).toBeTruthy());
  const buttons = screen.getAllByRole("button", { name: /已解决/ });
  fireEvent.click(buttons[buttons.length - 1]);

  await waitFor(() =>
    expect(api.decideFinding).toHaveBeenCalledWith(3, { status: "RESOLVED", note: null }),
  );
});

it("切换运行会重新加载候选/问题", async () => {
  api.listDiscoveryRuns.mockResolvedValue({
    rows: [{ ...RUN, id: 7 }, { ...RUN, id: 8 }],
    total: 2,
  });
  renderPage();

  await waitFor(() => expect(api.listCandidates).toHaveBeenCalledWith({ runId: 7 }));
  const combos = document.querySelectorAll(".ant-select-selector");
  fireEvent.mouseDown(combos[0] as Element);
  const options = await screen.findAllByText(/#8/);
  fireEvent.click(options[options.length - 1]);

  await waitFor(() => expect(api.listCandidates).toHaveBeenCalledWith({ runId: 8 }));
});
