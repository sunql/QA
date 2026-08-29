import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import EmbeddingProvidersPage from "../pages/EmbeddingProvidersPage";
import type { EmbeddingProvider } from "../types/embeddingProvider";

// vi.mock 工厂会被提升到顶部，用 vi.hoisted 保证 mockProvider 先初始化
const { mockProvider } = vi.hoisted(() => ({
  mockProvider: {
    id: 1,
    name: "Ollama bge-m3",
    providerType: "ollama",
    baseUrl: "http://localhost:11434/v1",
    modelName: "bge-m3:latest",
    dimension: 1024,
    isActive: true,
    createdTime: "2026-08-13T10:00:00Z",
    updatedTime: "2026-08-13T10:00:00Z",
  } as EmbeddingProvider,
}));

const api = vi.hoisted(() => ({
  listEmbeddingProviders: vi.fn(),
  createEmbeddingProvider: vi.fn(),
  updateEmbeddingProvider: vi.fn(),
  activateEmbeddingProvider: vi.fn(),
  deactivateEmbeddingProvider: vi.fn(),
}));

vi.mock("../api/embeddingProviders", () => api);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/embeddings"]}>
        <Routes>
          <Route path="/embeddings" element={<EmbeddingProvidersPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("EmbeddingProvidersPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listEmbeddingProviders.mockResolvedValue([mockProvider]);
  });

  it("渲染标题与表格，并加载服务列表", async () => {
    renderPage();

    expect(screen.getByText("Embedding 服务管理")).toBeInTheDocument();
    expect(screen.getByText("新增服务")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("Ollama bge-m3")).toBeInTheDocument();
      expect(screen.getByText("http://localhost:11434/v1")).toBeInTheDocument();
      expect(screen.getByText("bge-m3:latest")).toBeInTheDocument();
    });
  });

  it("渲染操作列按钮（激活行有停用，无启用）", async () => {
    renderPage();
    // antd 对无 icon 的两字中文按钮自动插入空格（"编 辑"/"停 用"），用正则容错
    await waitFor(() => {
      expect(screen.getByText(/编\s?辑/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /停\s?用/ })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /启\s?用/ })).not.toBeInTheDocument();
    });
  });

  it("非激活行显示启用按钮", async () => {
    api.listEmbeddingProviders.mockResolvedValue([{ ...mockProvider, isActive: false }]);
    renderPage();
    await waitFor(() => expect(screen.getByText("Ollama bge-m3")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /启\s?用/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /停\s?用/ })).not.toBeInTheDocument();
  });

  it("点击启用调用 activateEmbeddingProvider", async () => {
    const user = userEvent.setup();
    api.activateEmbeddingProvider.mockResolvedValue({ ...mockProvider, isActive: true });
    api.listEmbeddingProviders.mockResolvedValue([{ ...mockProvider, isActive: false }]);
    renderPage();
    await waitFor(() => expect(screen.getByText("Ollama bge-m3")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /启\s?用/ }));

    await waitFor(() => {
      expect(api.activateEmbeddingProvider).toHaveBeenCalledWith(1);
    });
  });

  it("点击停用并在确认后调用 deactivateEmbeddingProvider", async () => {
    const user = userEvent.setup();
    api.deactivateEmbeddingProvider.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() => expect(screen.getByText("Ollama bge-m3")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /停\s?用/ }));
    const confirmBtns = screen.getAllByRole("button", { name: /确\s?定$/ });
    await user.click(confirmBtns[confirmBtns.length - 1]);

    await waitFor(() => {
      expect(api.deactivateEmbeddingProvider).toHaveBeenCalledWith(1);
    });
  });

  it("点击新增并提交调用 createEmbeddingProvider", async () => {
    const user = userEvent.setup();
    api.createEmbeddingProvider.mockResolvedValue({ ...mockProvider, id: 2, name: "oMLX bge-m3 FP16" });
    renderPage();
    await waitFor(() => expect(screen.getByText("Ollama bge-m3")).toBeInTheDocument());

    await user.click(screen.getByText("新增服务"));
    const nameInputs = screen.getAllByRole("textbox");
    await user.type(nameInputs[0], "oMLX bge-m3 FP16");
    await user.type(nameInputs[1], "http://localhost:8888/v1");
    await user.type(nameInputs[2], "bge-m3-mlx-fp16");

    const okBtn = screen.getByRole("button", { name: /确\s?定$/ });
    await user.click(okBtn);

    await waitFor(() => {
      expect(api.createEmbeddingProvider).toHaveBeenCalledTimes(1);
      const payload = api.createEmbeddingProvider.mock.calls[0][0];
      expect(payload.name).toBe("oMLX bge-m3 FP16");
      expect(payload.baseUrl).toBe("http://localhost:8888/v1");
      expect(payload.modelName).toBe("bge-m3-mlx-fp16");
      expect(payload.dimension).toBe(1024);
    });
  });

  it("点击编辑打开表单并提交调用 updateEmbeddingProvider（apiKey 留空不传）", async () => {
    const user = userEvent.setup();
    api.updateEmbeddingProvider.mockResolvedValue({ ...mockProvider });
    renderPage();
    await waitFor(() => expect(screen.getByText("Ollama bge-m3")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    const nameInput = screen.getByDisplayValue("Ollama bge-m3");
    expect(nameInput).toBeInTheDocument();
    // 不填 apiKey 即提交（编辑时 apiKey 留空表示不修改）
    await user.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.updateEmbeddingProvider).toHaveBeenCalledTimes(1);
      const payload = api.updateEmbeddingProvider.mock.calls[0][1];
      expect(payload.modelName).toBe("bge-m3:latest");
      expect(payload.apiKey).toBeUndefined();
    });
  });
});
