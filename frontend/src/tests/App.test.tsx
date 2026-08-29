import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "../App";

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
  },
}));
const api = vi.hoisted(() => ({ listModels: vi.fn().mockResolvedValue([mockModel]) }));
vi.mock("../api/modelConfig", () => ({
  ...api,
  createModel: vi.fn(),
  updateModel: vi.fn(),
  deactivateModel: vi.fn(),
}));

describe("App 路由", () => {
  it("默认重定向到 /models 并渲染模型配置页", async () => {
    render(
      <ConfigProvider locale={zhCN}>
        <MemoryRouter initialEntries={["/"]}>
          <App />
        </MemoryRouter>
      </ConfigProvider>
    );
    // AppLayout 标题存在
    expect(screen.getByText("AI服务平台")).toBeInTheDocument();
    // 重定向后渲染 ModelConfigPage，标题出现
    expect(screen.getByText("模型配置管理")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("deepseek-chat")).toBeInTheDocument());
  });
});
