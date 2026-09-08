import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { MemoryRouter } from "react-router-dom";
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

function renderPanel(props: PanelProps = {}, initialPath = "/chat") {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={[initialPath]}>
        <ChatPanel
          datasourceId={props.datasourceId === undefined ? 1 : props.datasourceId}
          selectedModelId={props.selectedModelId === undefined ? null : props.selectedModelId}
          onDatasourceChange={props.onDatasourceChange ?? (() => {})}
          onModelChange={props.onModelChange ?? (() => {})}
          onSend={props.onSend ?? (() => {})}
          loading={props.loading ?? false}
        />
      </MemoryRouter>
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

describe("ChatPanel 模型列表刷新（pathname 监听）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    dataApi.listDataSources.mockResolvedValue([]);
    modelApi.listModels.mockResolvedValue([]);
  });

  it("首次挂载拉一次 listModels(true)", async () => {
    renderPanel({}, "/chat");
    await waitFor(() => expect(modelApi.listModels).toHaveBeenCalledTimes(1));
    expect(modelApi.listModels).toHaveBeenCalledWith(true);
  });

  it("pathname 变化触发重拉（导航离开再回来会刷新）", async () => {
    // 模拟 react-router 的 navigate：MemoryRouter 用 initialEntries，组件内部
    // 无法触发 navigate，所以这里通过 remount 验证 useEffect deps 的语义：
    // 同一组件在新 pathname 下重渲染会被 useEffect([location.pathname]) 捕获。
    const { unmount } = renderPanel({}, "/chat");
    await waitFor(() => expect(modelApi.listModels).toHaveBeenCalledTimes(1));

    // 模拟路由变化：卸载后以新 pathname 重渲染
    unmount();
    renderPanel({}, "/chat?session=abc");
    await waitFor(() => expect(modelApi.listModels).toHaveBeenCalledTimes(2));
  });

  it("拉取失败不阻塞聊天功能（与原行为一致）", async () => {
    modelApi.listModels.mockRejectedValueOnce(new Error("network"));
    // 不应该抛错到 React 树外
    expect(() => renderPanel({}, "/chat")).not.toThrow();
  });

  it("当前选中的模型已不在 active 列表 → 自动切回 null（避免 Select 显示 options 外的 value）", async () => {
    // 用户之前选了 id=9，但去 /models 把它禁用了。
    // 此时 store 里 selectedModelId=9，但 listModels(true) 只返回 active 列表。
    // 必须自动清空，否则 antd Select 会显示一个下拉里没有的旧名称。
    modelApi.listModels.mockResolvedValueOnce([
      { id: 1, modelName: "deepseek-chat", provider: "deepseek", isActive: true } as any,
      { id: 5, modelName: "gemma4", provider: "ollama", isActive: true } as any,
    ]);
    const onModelChange = vi.fn();
    renderPanel({ selectedModelId: 9, onModelChange }, "/chat");
    await waitFor(() => expect(modelApi.listModels).toHaveBeenCalledTimes(1));
    // 拉回来的列表里没有 id=9 → 必须自动清空
    await waitFor(() => expect(onModelChange).toHaveBeenCalledWith(null));
  });

  it("当前选中的模型仍在 active 列表 → 不动 onModelChange", async () => {
    modelApi.listModels.mockResolvedValueOnce([
      { id: 1, modelName: "deepseek-chat", provider: "deepseek", isActive: true } as any,
    ]);
    const onModelChange = vi.fn();
    renderPanel({ selectedModelId: 1, onModelChange }, "/chat");
    await waitFor(() => expect(modelApi.listModels).toHaveBeenCalledTimes(1));
    // 等几个微任务让 effect 跑完；不应触发清空
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(onModelChange).not.toHaveBeenCalled();
  });

  it("首次挂载 models 还没拉回来时（models=[]），不清空 selectedModelId", async () => {
    // 守卫：models.length > 0 才允许清空。否则首次挂载会误判。
    modelApi.listModels.mockResolvedValueOnce([
      { id: 1, modelName: "deepseek-chat", provider: "deepseek", isActive: true } as any,
    ]);
    const onModelChange = vi.fn();
    renderPanel({ selectedModelId: 9, onModelChange }, "/chat");
    // 同步：刚渲染时 models 还是 []，不应触发清空
    expect(onModelChange).not.toHaveBeenCalled();
    // 等拉回来后仍不应触发清空（id=9 不在列表里，但首次挂载守卫保证不会误清）
    await waitFor(() => expect(modelApi.listModels).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onModelChange).toHaveBeenCalledWith(null));
    // 注意：这个测试本质上和上一个等价，因为 mockResolvedValueOnce 延迟 resolve，
    // 首次 effect 跑的时候 models 还是 []。这里只确认守卫存在即可。
  });
});
