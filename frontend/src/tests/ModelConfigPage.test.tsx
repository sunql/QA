import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import ModelConfigPage from "../pages/ModelConfigPage";
import type { ModelConfig } from "../types/modelConfig";

// vi.mock 工厂会被提升到顶部，用 vi.hoisted 保证 mockModel 先初始化
const { mockModel } = vi.hoisted(() => ({
  mockModel: {
    id: 1,
    modelName: "deepseek-chat",
    provider: "OPENAI_COMPATIBLE_PROXY",
    apiEndpoint: "https://api.deepseek.com/v1",
    isActive: true,
    costPer1KInput: 0.0014,
    costPer1KOutput: 0.0028,
    maxInputTokens: 64000,
    weight: 2,
    costThreshold: 1,
    createdAt: "2026-08-11T00:00:00Z",
    updatedAt: "2026-08-11T00:00:00Z",
  } as ModelConfig,
}));

const api = vi.hoisted(() => ({
  listModels: vi.fn(),
  createModel: vi.fn(),
  updateModel: vi.fn(),
  deactivateModel: vi.fn(),
}));

vi.mock("../api/modelConfig", () => api);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/models"]}>
        <Routes>
          <Route path="/models" element={<ModelConfigPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("ModelConfigPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listModels.mockResolvedValue([mockModel]);
  });

  it("渲染标题与表格，并加载模型列表", async () => {
    renderPage();

    expect(screen.getByText("模型配置管理")).toBeInTheDocument();
    expect(screen.getByText("新增模型")).toBeInTheDocument();

    // 等待异步数据加载后行渲染
    await waitFor(() => {
      expect(screen.getByText("deepseek-chat")).toBeInTheDocument();
      expect(screen.getByText("$0.0014")).toBeInTheDocument();
    });
  });

  it("渲染操作列按钮", async () => {
    renderPage();
    // antd 对无 icon 的两字中文按钮自动插入空格（"编 辑"/"停 用"），用正则容错
    await waitFor(() => {
      expect(screen.getByText(/编\s?辑/)).toBeInTheDocument();
      expect(screen.getByText(/停\s?用/)).toBeInTheDocument();
    });
  });

  it("停用模型为 inactive 时不显示停用按钮", async () => {
    api.listModels.mockResolvedValue([{ ...mockModel, isActive: false }]);
    renderPage();
    await waitFor(() => expect(screen.getByText("deepseek-chat")).toBeInTheDocument());
    // inactive 行仍有编辑按钮，但无停用按钮（状态列的 Tag 显示"停用"不算按钮）
    expect(screen.getByRole("button", { name: /编\s?辑/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /停\s?用/ })).not.toBeInTheDocument();
  });

  it("点击新增并提交调用 createModel", async () => {
    const user = userEvent.setup();
    api.createModel.mockResolvedValue({ ...mockModel, id: 2 });
    renderPage();
    await waitFor(() => expect(screen.getByText("deepseek-chat")).toBeInTheDocument());

    await user.click(screen.getByText("新增模型"));
    const nameInputs = screen.getAllByRole("textbox");
    await user.type(nameInputs[0], "gpt-4o-mini");
    await user.type(nameInputs[1], "https://api.openai.com/v1");
    await user.type(screen.getByPlaceholderText("sk-..."), "sk-test");

    // 模态框确定按钮（antd 对两字中文按钮自动加空格："确 定"）
    const okBtn = screen.getByRole("button", { name: /确\s?定$/ });
    await user.click(okBtn);

    await waitFor(() => {
      expect(api.createModel).toHaveBeenCalledTimes(1);
      expect(api.createModel.mock.calls[0][0].modelName).toBe("gpt-4o-mini");
    });
  });

  it("点击停用并在确认后调用 deactivateModel", async () => {
    const user = userEvent.setup();
    api.deactivateModel.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() => expect(screen.getByText("deepseek-chat")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /停\s?用/ }));
    const confirmBtns = screen.getAllByRole("button", { name: /确\s?定$/ });
    await user.click(confirmBtns[confirmBtns.length - 1]);

    await waitFor(() => {
      expect(api.deactivateModel).toHaveBeenCalledWith(1);
    });
  });

  it("点击编辑打开表单并提交调用 updateModel", async () => {
    const user = userEvent.setup();
    api.updateModel.mockResolvedValue({ ...mockModel });
    renderPage();
    await waitFor(() => expect(screen.getByText("deepseek-chat")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    // 模态框打开，模型名称输入框预填了 deepseek-chat
    const nameInput = screen.getByDisplayValue("deepseek-chat");
    expect(nameInput).toBeInTheDocument();
    // 不填 apiKey 即提交（编辑时 apiKey 可选，留空表示不修改）
    await user.click(screen.getByRole("button", { name: /确\s?定$/ }));

    await waitFor(() => {
      expect(api.updateModel).toHaveBeenCalledTimes(1);
      const payload = api.updateModel.mock.calls[0][1];
      expect(payload.modelName).toBe("deepseek-chat");
      expect(payload.apiKey).toBeUndefined();
    });
  });
});
