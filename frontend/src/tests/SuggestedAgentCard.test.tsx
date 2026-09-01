import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import SuggestedAgentCard from "../components/chat/SuggestedAgentCard";
import type { AgentSuggestion } from "../types/chat";

const suggestion: AgentSuggestion = {
  recommendedAgentCode: "SUPPLIER_RISK_AGENT",
  confidence: 0.8,
  reason: "检测到风险评估类诉求，推荐风险健康度评估",
};

describe("SuggestedAgentCard", () => {
  it("渲染推荐 Agent 编码 + 置信度百分比 + 理由", () => {
    render(
      <ConfigProvider>
        <SuggestedAgentCard data={suggestion} />
      </ConfigProvider>
    );
    expect(screen.getByText("建议使用 Agent")).toBeInTheDocument();
    expect(screen.getByText("SUPPLIER_RISK_AGENT")).toBeInTheDocument();
    expect(screen.getByText("置信度: 80%")).toBeInTheDocument();
    expect(
      screen.getByText("检测到风险评估类诉求，推荐风险健康度评估")
    ).toBeInTheDocument();
  });

  it("置信度 0.4 展示 40%（四舍五入到整百分比）", () => {
    render(
      <ConfigProvider>
        <SuggestedAgentCard
          data={{ ...suggestion, confidence: 0.4 }}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("置信度: 40%")).toBeInTheDocument();
  });

  it("提示用户可直接输入显式指名执行", () => {
    render(
      <ConfigProvider>
        <SuggestedAgentCard data={suggestion} />
      </ConfigProvider>
    );
    expect(screen.getByText(/可直接输入/)).toBeInTheDocument();
  });
});
