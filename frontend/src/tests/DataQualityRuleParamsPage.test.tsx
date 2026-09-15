/** DataQualityRuleParamsPage — 规则参数结构化配置页（feat-dq-rule-params Task 11） */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../i18n";
import { DataQualityRuleParamsPage } from "../pages/DataQualityRuleParamsPage";

const api = vi.hoisted(() => ({
  listRules: vi.fn().mockResolvedValue([]),
  createRule: vi.fn().mockResolvedValue({}),
  getRule: vi.fn().mockResolvedValue(null),
  updateRule: vi.fn().mockResolvedValue({}),
  deleteRule: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("../api/dataQualityRuleParams", () => api);

describe("DataQualityRuleParamsPage", () => {
  beforeEach(() => {
    i18n.changeLanguage("en-US");
  });

  it("renders title", () => {
    render(
      <I18nextProvider i18n={i18n}>
        <DataQualityRuleParamsPage />
      </I18nextProvider>,
    );
    expect(screen.getByText(/Rule Config \(Structured\)/i)).toBeInTheDocument();
  });
});
