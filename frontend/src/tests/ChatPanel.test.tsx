import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import ChatPanel from "../components/chat/ChatPanel";

const chatApi = vi.hoisted(() => ({
  getSuggestions: vi.fn(),
}));
const dataApi = vi.hoisted(() => ({
  listDataSources: vi.fn(),
}));
const modelApi = vi.hoisted(() => ({
  listModels: vi.fn(),
}));

vi.mock("../api/chat", () => chatApi);
vi.mock("../api/datasource", () => dataApi);
vi.mock("../api/modelConfig", () => modelApi);

interface PanelProps {
  datasourceId?: number | null;
  selectedModelId?: number | null;
  onDatasourceChange?: (id: number | null) => void;
  onModelChange?: (id: number | null) => void;
  onSend?: (question: string, chartType: import("../types/chat").ChartType | null) => void;
  loading?: boolean;
}

function renderPanel(props: PanelProps = {}) {
  return render(
    <ConfigProvider locale={zhCN}>
      <ChatPanel
        datasourceId={props.datasourceId === undefined ? 1 : props.datasourceId}
        selectedModelId={props.selectedModelId === undefined ? null : props.selectedModelId}
        onDatasourceChange={props.onDatasourceChange ?? (() => {})}
        onModelChange={props.onModelChange ?? (() => {})}
        onSend={props.onSend ?? (() => {})}
        loading={props.loading ?? false}
      />
    </ConfigProvider>
  );
}

describe("ChatPanel 相似问题建议", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    dataApi.listDataSources.mockResolvedValue([]);
    modelApi.listModels.mockResolvedValue([]);
    chatApi.getSuggestions.mockResolvedValue([
      { question: "各供应商的收货数量汇总", sql: "SELECT 1", similarity: 0.92 },
    ]);
  });

  it("输入停顿后（防抖）请求相似问法并渲染 chips，点击回填输入框", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByPlaceholderText(/输入自然语言问题/), "各供应商");

    // 防抖 300ms 后触发检索
    await waitFor(
      () => expect(chatApi.getSuggestions).toHaveBeenCalledTimes(1),
      { timeout: 1500 }
    );
    expect(chatApi.getSuggestions).toHaveBeenCalledWith("各供应商", 1);

    const chip = await screen.findByRole("button", { name: /各供应商的收货数量汇总/ });
    await user.click(chip);
    const textarea = screen.getByPlaceholderText(/输入自然语言问题/) as HTMLTextAreaElement;
    expect(textarea.value).toBe("各供应商的收货数量汇总");
  });

  it("发送消息后清空建议 chips", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    renderPanel({ onSend });
    await user.type(screen.getByPlaceholderText(/输入自然语言问题/), "各供应商");
    const chip = await screen.findByRole("button", { name: /各供应商的收货数量汇总/ });
    expect(chip).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /发\s?送/ }));
    expect(onSend).toHaveBeenCalledTimes(1);
    // 问题已清空，建议随之消失
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /各供应商的收货数量汇总/ })
      ).not.toBeInTheDocument()
    );
  });

  it("输入为空时（含无数据源）不请求相似问法", async () => {
    renderPanel();
    // 空问题：不请求
    expect(chatApi.getSuggestions).not.toHaveBeenCalled();
    // 数据源为空：即使有输入也不请求
    renderPanel({ datasourceId: null });
    const user = userEvent.setup();
    await user.type(screen.getAllByPlaceholderText(/输入自然语言问题/)[1], "各供应商");
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(chatApi.getSuggestions).not.toHaveBeenCalled();
  });

  // 回归测试：后端偶发返回相同 question 的多条建议时，不应触发 React duplicate key 警告
  // （前端 console.error spy 监听；通过即代表 key 唯一）
  it("后端返回重复 question 的多条建议 → 不产生 duplicate key 警告", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    chatApi.getSuggestions.mockResolvedValueOnce([
      { question: "查 2026 年同期", sql: null, similarity: 0.91 },
      { question: "查 2026 年同期", sql: "SELECT 2", similarity: 0.88 },
      { question: "查 2026 年同期", sql: null, similarity: 0.85 },
    ]);
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByPlaceholderText(/输入自然语言问题/), "查");
    await waitFor(
      () => expect(chatApi.getSuggestions).toHaveBeenCalledTimes(1),
      { timeout: 1500 }
    );
    // 等 chips 渲染（3 条同名建议都应被找到，用 findAllByRole 而非 findByRole）
    const chips = await screen.findAllByRole("button", { name: /查 2026 年同期/ });
    expect(chips).toHaveLength(3);
    const duplicateKeyCalls = errorSpy.mock.calls.filter((call) =>
      String(call[0] ?? "").includes("two children with the same key")
    );
    expect(duplicateKeyCalls).toHaveLength(0);
    errorSpy.mockRestore();
  });
});
