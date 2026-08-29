import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import ServiceStatusPage from "../pages/ServiceStatusPage";
import type { ServiceStatusResponse } from "../types/serviceStatus";

// vi.mock 工厂会被提升到顶部，用 vi.hoisted 保证 mockResponse / api 先初始化
const { mockResponse } = vi.hoisted(() => ({
  mockResponse: {
    services: [
      {
        name: "postgresql",
        status: "up",
        latencyMs: 12,
        endpoint: "postgresql+asyncpg://qa_user@localhost:5432/qa_metadata",
        detail: null,
      },
      {
        name: "neo4j",
        status: "up",
        latencyMs: 8,
        endpoint: "bolt://localhost:7687",
        detail: null,
      },
      {
        name: "milvus",
        status: "down",
        latencyMs: 20,
        endpoint: "http://localhost:19530",
        detail: "milvus unreachable",
      },
      {
        name: "embedding",
        status: "not_configured",
        latencyMs: null,
        endpoint: null,
        detail: "未配置 Embedding 端点（EMBEDDING_API_BASE 或激活的 provider）",
      },
    ],
    checkedAt: "2026-08-14T08:00:00Z",
  } as ServiceStatusResponse,
}));

const api = vi.hoisted(() => ({
  getServiceStatus: vi.fn(),
}));

vi.mock("../api/serviceStatus", () => api);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/status"]}>
        <Routes>
          <Route path="/status" element={<ServiceStatusPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("ServiceStatusPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getServiceStatus.mockResolvedValue(mockResponse);
  });

  it("渲染四项服务卡片并加载状态", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("PostgreSQL 元数据库")).toBeInTheDocument();
      expect(screen.getByText("Neo4j 图库")).toBeInTheDocument();
      expect(screen.getByText("Milvus 向量库")).toBeInTheDocument();
      expect(screen.getByText("Embedding 服务")).toBeInTheDocument();
    });

    // 状态标签：up x2、down x1、not_configured x1
    expect(screen.getAllByText("正常")).toHaveLength(2);
    expect(screen.getByText("异常")).toBeInTheDocument();
    expect(screen.getByText("未配置")).toBeInTheDocument();

    // 延迟与端点
    expect(screen.getByText("延迟 12 ms")).toBeInTheDocument();
    expect(screen.getByText("bolt://localhost:7687")).toBeInTheDocument();

    // down 的 detail 与探测时间
    expect(screen.getByText("milvus unreachable")).toBeInTheDocument();
    expect(screen.getByText(/探测时间/)).toBeInTheDocument();
  });

  it("点击刷新按钮重新探测", async () => {
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("PostgreSQL 元数据库")).toBeInTheDocument();
    });
    expect(api.getServiceStatus).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /刷\s?新/ }));

    await waitFor(() => {
      expect(api.getServiceStatus).toHaveBeenCalledTimes(2);
    });
  });
});
