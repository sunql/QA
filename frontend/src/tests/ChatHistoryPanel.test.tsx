import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import ChatHistoryPanel from "../components/chat/ChatHistoryPanel";
import type { ChatSession } from "../types/chatHistory";

function makeSession(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    sessionId: "s-default",
    firstTime: "2026-01-01T00:00:00Z",
    lastTime: "2026-01-01T01:00:00Z",
    messageCount: 2,
    lastQuestion: "默认问题",
    lastAnswerPreview: "默认回答",
    ...overrides,
  };
}

interface PanelProps {
  sessions?: ChatSession[];
  currentSessionId?: string | null;
  loading?: boolean;
  error?: string | null;
  onSelect?: (sessionId: string) => void;
  onDelete?: (sessionId: string) => Promise<void> | void;
  onNewChat?: () => void;
  onCollapse?: () => void;
}

function renderPanel(props: PanelProps = {}) {
  const onSelect = props.onSelect ?? vi.fn();
  const onDelete = props.onDelete ?? vi.fn();
  const onNewChat = props.onNewChat ?? vi.fn();
  const onCollapse = props.onCollapse ?? vi.fn();
  return {
    onSelect,
    onDelete,
    onNewChat,
    onCollapse,
    ...render(
      <ConfigProvider locale={zhCN}>
        <ChatHistoryPanel
          sessions={props.sessions ?? []}
          currentSessionId={props.currentSessionId ?? null}
          loading={props.loading ?? false}
          error={props.error ?? null}
          onSelect={onSelect}
          onDelete={onDelete}
          onNewChat={onNewChat}
          onCollapse={onCollapse}
        />
      </ConfigProvider>
    ),
  };
}

describe("ChatHistoryPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("渲染 sessions 列表（3 项）", () => {
    const sessions = [
      makeSession({ sessionId: "s1", lastQuestion: "第一个问题" }),
      makeSession({ sessionId: "s2", lastQuestion: "第二个问题" }),
      makeSession({ sessionId: "s3", lastQuestion: "第三个问题" }),
    ];
    renderPanel({ sessions });
    expect(screen.getByText("第一个问题")).toBeInTheDocument();
    expect(screen.getByText("第二个问题")).toBeInTheDocument();
    expect(screen.getByText("第三个问题")).toBeInTheDocument();
  });

  it("空状态显示 Empty 占位", () => {
    renderPanel({ sessions: [] });
    expect(screen.getByText("暂无历史对话")).toBeInTheDocument();
  });

  it("loading=true 时显示 Spin（不显示 Empty）", () => {
    const { container } = renderPanel({ sessions: [], loading: true });
    expect(screen.queryByText("暂无历史对话")).not.toBeInTheDocument();
    // antd Spin 渲染 .ant-spin 容器
    expect(container.querySelector(".ant-spin")).toBeInTheDocument();
  });

  it("error 非空时显示 Alert（不显示 Empty）", () => {
    renderPanel({ sessions: [], error: "加载历史失败" });
    expect(screen.getByText("加载历史失败")).toBeInTheDocument();
    expect(screen.queryByText("暂无历史对话")).not.toBeInTheDocument();
  });

  it("点击列表项触发 onSelect(sessionId)", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    renderPanel({
      sessions: [
        makeSession({ sessionId: "s-target", lastQuestion: "目标问题" }),
      ],
      onSelect,
    });
    await user.click(screen.getByText("目标问题"));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("s-target");
  });

  it("删除按钮：点击先弹 Popconfirm，确认后调 onDelete(sessionId)", async () => {
    const user = userEvent.setup();
    const onDelete = vi.fn().mockResolvedValue(undefined);
    renderPanel({
      sessions: [
        makeSession({ sessionId: "s-del", lastQuestion: "要删的问题" }),
      ],
      onDelete,
    });
    // antd List item 操作区有删除按钮：找到 aria-label 或 title 为「删除」的按钮
    const deleteBtn = screen.getByRole("button", { name: /删除/ });
    await user.click(deleteBtn);
    // 弹 Popconfirm，文案含「确定删除该会话吗？」
    await waitFor(() => {
      expect(screen.getByText("确定删除该会话吗？")).toBeInTheDocument();
    });
    // 点击 Popconfirm 的「确定」按钮（antd 在 icon 与文字间可能插入空白）
    const okButtons = screen.getAllByRole("button", { name: /确\s?定/ });
    await user.click(okButtons[okButtons.length - 1]);
    await waitFor(() => {
      expect(onDelete).toHaveBeenCalledWith("s-del");
    });
  });

  it("选中态：currentSessionId 匹配时该项背景高亮", () => {
    const sessions = [
      makeSession({ sessionId: "active", lastQuestion: "当前会话" }),
      makeSession({ sessionId: "other", lastQuestion: "其他会话" }),
    ];
    const { container } = renderPanel({
      sessions,
      currentSessionId: "active",
    });
    // 选中项与未选中项的 List item DOM 应有不同样式
    const items = container.querySelectorAll(".ant-list-item");
    expect(items.length).toBe(2);
    const activeStyle = (items[0] as HTMLElement).getAttribute("style") ?? "";
    const otherStyle = (items[1] as HTMLElement).getAttribute("style") ?? "";
    // React 把 hex 转 rgb：#e6f4ff → rgb(230, 244, 255)；#1677ff → rgb(22, 119, 255)
    expect(activeStyle).toContain("rgb(230, 244, 255)");
    expect(activeStyle).toContain("rgb(22, 119, 255)");
    // 未选中项：不含蓝色实色
    expect(otherStyle).not.toContain("rgb(230, 244, 255)");
    expect(otherStyle).not.toContain("rgb(22, 119, 255)");
  });

  it("「新对话」按钮触发 onNewChat", async () => {
    const user = userEvent.setup();
    const onNewChat = vi.fn();
    renderPanel({ onNewChat });
    await user.click(screen.getByRole("button", { name: /新对话/ }));
    expect(onNewChat).toHaveBeenCalledTimes(1);
  });
});