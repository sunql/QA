/** 会话历史侧栏：搜索 / 按场景分组 / 置顶（2026-11-24 增强）。 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import i18n from "../i18n/i18n";
import ConversationHistoryPanel from "../components/copilot/ConversationHistoryPanel";
import type { CopilotConversationSummary } from "../api/procurementCopilot";

function item(
  conversationId: string,
  lastQuestion: string,
  agentCode = "PROCUREMENT_SOURCING",
): CopilotConversationSummary {
  return {
    conversationId,
    agentCode,
    modelConfigId: 3,
    turnCount: 2,
    lastQuestion,
    updatedAt: "2026-11-23T10:00:00Z",
  };
}

function renderPanel(props: {
  conversations: CopilotConversationSummary[];
  pinnedIds?: string[];
  agentNames?: Record<string, string>;
  onTogglePin?: (id: string) => void;
}) {
  const onTogglePin = props.onTogglePin ?? vi.fn();
  render(
    <I18nextProvider i18n={i18n}>
      <ConfigProvider locale={zhCN}>
        <ConversationHistoryPanel
          conversations={props.conversations}
          currentConversationId={undefined}
          loading={false}
          pinnedIds={props.pinnedIds ?? []}
          agentNames={props.agentNames ?? { PROCUREMENT_SOURCING: "采购寻源" }}
          onSelect={vi.fn()}
          onDelete={vi.fn()}
          onTogglePin={onTogglePin}
          onNewConversation={vi.fn()}
          onCollapse={vi.fn()}
        />
      </ConfigProvider>
    </I18nextProvider>,
  );
  return { onTogglePin };
}

describe("ConversationHistoryPanel 搜索", () => {
  it("输入关键词只保留匹配的会话", () => {
    renderPanel({
      conversations: [item("c-1", "不锈钢板供应商"), item("c-2", "报价对比")],
    });
    expect(screen.getByText("不锈钢板供应商")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("搜索历史对话"), {
      target: { value: "报价" },
    });
    expect(screen.queryByText("不锈钢板供应商")).toBeNull();
    expect(screen.getByText("报价对比")).toBeTruthy();
  });
});

describe("ConversationHistoryPanel 分组", () => {
  it("按场景分组显示组名", () => {
    renderPanel({
      conversations: [
        item("c-1", "问题A", "PROCUREMENT_SOURCING"),
        item("c-2", "问题B", "PROCUREMENT_SUPPLIER_SEARCH_AGENT"),
      ],
      agentNames: {
        PROCUREMENT_SOURCING: "采购寻源",
        PROCUREMENT_SUPPLIER_SEARCH_AGENT: "供应商检索",
      },
    });
    expect(screen.getByText("采购寻源")).toBeTruthy();
    expect(screen.getByText("供应商检索")).toBeTruthy();
  });
});

describe("ConversationHistoryPanel 置顶", () => {
  it("点击图钉 ⇒ 回调 onTogglePin；置顶项显示在「置顶」组", () => {
    const { onTogglePin } = renderPanel({
      conversations: [item("c-1", "问题A"), item("c-2", "问题B")],
      pinnedIds: ["c-2"],
    });
    expect(screen.getByText("置顶")).toBeTruthy();
    // 置顶组在前 ⇒ 第一个按钮属已置顶的 c-2，第二个属 c-1
    const pinButtons = screen.getAllByLabelText("置顶会话");
    fireEvent.click(pinButtons[1]);
    expect(onTogglePin).toHaveBeenCalledWith("c-1");
  });
});
