import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import AgentResponseCard from "../components/chat/AgentResponseCard";
import type { AgentRunRead } from "../types/agentRuntime";

const supplierRiskResult = {
  profile: {
    enterpriseKey: 100001,
    enterpriseCode: "SUP000001",
    owner: "procurement",
    matchRule: "MDM_MASTER",
    effectiveDate: "2026-01-01",
    expiryDate: null,
  },
  level: "high",
  levelSource: "risk_score",
  contributions: [
    {
      featureName: "SUPPLIER_RISK_SCORE",
      featureAlias: "综合风险评分",
      value: "0.50",
      unit: "score",
      threshold: "0.80",
      passed: false,
      note: "综合风险评分",
    },
  ],
  riskPoints: "供应商综合风险评分偏低，建议重点关注。",
  riskPointsSource: "fallback_template",
  recommendedActions: ["加大质量抽检频次", "关注交付稳定性"],
  tokensUsed: 0,
  promptTokens: 0,
  completionTokens: 0,
  cost: 0,
  llmModelName: null,
  fetchedAt: "2026-08-31T00:00:00Z",
};

function makeRun(overrides: Partial<AgentRunRead> = {}): AgentRunRead {
  return {
    agentCode: "SUPPLIER_RISK_AGENT",
    agentName: "供应商风险 Agent",
    agentOwner: "procurement",
    tool: "supplier_risk",
    result: supplierRiskResult,
    answer: "供应商 **SUP000001** 风险等级：high。",
    tokensUsed: 0,
    promptTokens: 0,
    completionTokens: 0,
    cost: 0,
    llmModelName: null,
    executedAt: "2026-08-31T00:00:00Z",
    ...overrides,
  };
}

describe("AgentResponseCard", () => {
  it("渲染头部（agentName / tool / owner / code）", () => {
    render(
      <ConfigProvider>
        <AgentResponseCard data={makeRun()} />
      </ConfigProvider>
    );
    expect(screen.getByText("供应商风险 Agent")).toBeInTheDocument();
    expect(screen.getByText("supplier_risk")).toBeInTheDocument();
    expect(screen.getByText("负责人：procurement")).toBeInTheDocument();
    expect(screen.getByText("SUPPLIER_RISK_AGENT")).toBeInTheDocument();
  });

  it("渲染 answer Markdown 加粗", () => {
    const { container } = render(
      <ConfigProvider>
        <AgentResponseCard data={makeRun()} />
      </ConfigProvider>
    );
    const strong = container.querySelector("strong");
    expect(strong).not.toBeNull();
    expect(strong?.textContent).toBe("SUP000001");
  });

  it("supplier_risk 工具内嵌 SupplierRiskCard（风险等级 + 贡献明细）", () => {
    render(
      <ConfigProvider>
        <AgentResponseCard data={makeRun()} />
      </ConfigProvider>
    );
    // SupplierRiskCard 头部的风险等级 Tag
    expect(screen.getByText("高风险")).toBeInTheDocument();
    // 贡献明细表格 featureAlias（表格列 + note 列各一次，用 getAllByText）
    expect(screen.getAllByText("综合风险评分").length).toBeGreaterThan(0);
  });

  it("未识别工具回退 JSON 明细折叠", () => {
    render(
      <ConfigProvider>
        <AgentResponseCard
          data={makeRun({ tool: "custom_tool", result: { hello: "world" } })}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("工具原始结果")).toBeInTheDocument();
  });

  it("渲染 Tokens / 成本 / LLM 模型标签", () => {
    render(
      <ConfigProvider>
        <AgentResponseCard
          data={makeRun({ tokensUsed: 12, cost: 0.000123, llmModelName: "deepseek-chat" })}
        />
      </ConfigProvider>
    );
    expect(screen.getByText("Tokens: 12 (P 0 / C 0)")).toBeInTheDocument();
    expect(screen.getByText("成本: $0.000123")).toBeInTheDocument();
    expect(screen.getByText("deepseek-chat")).toBeInTheDocument();
  });
});
