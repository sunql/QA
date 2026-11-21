/** M4-3 知识检索页 + 场景目录页测试（mock API，真实 i18n）。

判据要点：
1. **证据卡片渲染 M2-1 契约字段**（channel/confidence/security/releaseVersion）；
2. **场景目录不暴露 handlerRef**（能力面非实现面——DOM 断言）；
3. **可运行性标签**（runnable/notRunnable）；
4. **空态与降级**（目录空 / 通道降级 Alert）。
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { describe, expect, it, vi } from "vitest";
import i18n from "../../i18n/i18n";
import ProcurementKnowledgePage from "../ProcurementKnowledgePage";
import ProcurementAgentCatalogPage from "../ProcurementAgentCatalogPage";
import type { KnowledgeEvidence } from "../../api/procurementKnowledge";

const api = vi.hoisted(() => ({
  queryKnowledge: vi.fn(),
  searchEntities: vi.fn(),
  getGraphNeighbors: vi.fn(),
  searchProcurementDocuments: vi.fn(),
  getAgentCatalog: vi.fn(),
}));

vi.mock("../../api/procurementKnowledge", () => api);

function renderPage(node: React.ReactNode) {
  return render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        {node}
      </ConfigProvider>
    </I18nextProvider>,
  );
}

function makeEvidence(): KnowledgeEvidence {
  return {
    evidenceId: "ev-abc123",
    channel: "GRAPH",
    kind: "CLASS",
    ref: "DIM_SUPPLIER",
    title: "供应商",
    snippet: "供应商主数据类",
    releaseId: 7,
    releaseVersion: "v1.2",
    source: "RELEASE",
    confidence: 0.92,
    security: { level: "L1", scope: ["PROCUREMENT"] },
  };
}

// ── 知识检索页 ────────────────────────────────────────────────────────────

describe("ProcurementKnowledgePage", () => {
  it("统一查询渲染证据卡片（M2-1 契约字段可视化）", async () => {
    api.queryKnowledge.mockResolvedValue({
      question: "供应商",
      domain: "PROCUREMENT",
      entities: [],
      evidence: [makeEvidence()],
      routeResults: [
        { channel: "GRAPH", status: "OK", itemCount: 1, degradedReason: null },
      ],
    });
    renderPage(<ProcurementKnowledgePage />);
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "供应商" },
    });
    fireEvent.click(screen.getAllByRole("button", { name: /检\s*索/ })[0]);
    await waitFor(
      () => {
        expect(screen.getAllByText(/供应商/).length).toBeGreaterThan(0);
        expect(screen.getByText("0.92")).toBeTruthy();
      },
      { timeout: 3000 },
    );
    // 通道降级自证
    expect(screen.getByText(/GRAPH: OK/)).toBeTruthy();
  });

  it("零证据 ⇒ 空态提示（不冒充结果）", async () => {
    api.queryKnowledge.mockResolvedValue({
      question: "x",
      domain: "PROCUREMENT",
      entities: [],
      evidence: [],
      routeResults: [
        { channel: "RAG", status: "EMPTY", itemCount: 0, degradedReason: null },
      ],
    });
    renderPage(<ProcurementKnowledgePage />);
    fireEvent.change(screen.getByPlaceholderText(/不锈钢板/), {
      target: { value: "x" },
    });
    fireEvent.click(screen.getAllByRole("button", { name: /检\s*索/ })[0]);
    await waitFor(() => {
      expect(screen.getByText(/未命中证据/)).toBeTruthy();
    });
  });
});

// ── 场景目录页 ────────────────────────────────────────────────────────────

describe("ProcurementAgentCatalogPage", () => {
  it("目录渲染可运行性标签且不暴露 handlerRef", async () => {
    api.getAgentCatalog.mockResolvedValue([
      {
        agentCode: "PROCUREMENT_SUPPLIER_SEARCH_AGENT",
        agentName: "采购供应商检索 Agent",
        description: "供应商检索",
        status: "active",
        runnable: true,
        tool: {
          name: "supplier_search",
          description: "供应商检索双路",
          handlerRef: "supplier_search",
        },
      },
      {
        agentCode: "PROCUREMENT_COPILOT",
        agentName: "采购 AI 副驾",
        description: "占位",
        status: "active",
        runnable: false,
        tool: null,
      },
    ]);
    const { container } = renderPage(<ProcurementAgentCatalogPage />);
    await waitFor(() => {
      expect(screen.getByText("可运行")).toBeTruthy();
      expect(screen.getByText("未启用")).toBeTruthy();
      expect(screen.getByText("supplier_search")).toBeTruthy();
    });
    // 能力面非实现面：handlerRef 不得出现在 DOM
    expect(container.innerHTML.includes("handlerRef")).toBe(false);
    expect(container.innerHTML.includes("handler_ref")).toBe(false);
  });

  it("目录空 ⇒ 空态提示", async () => {
    api.getAgentCatalog.mockResolvedValue([]);
    renderPage(<ProcurementAgentCatalogPage />);
    await waitFor(() => {
      expect(screen.getByText(/目录为空/)).toBeTruthy();
    });
  });
});
