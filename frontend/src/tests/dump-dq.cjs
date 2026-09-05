// dump buttons in DataQualityPage for debugging
const { describe, it } = require("vitest");
const { render, screen } = require("@testing-library/react");
require("@testing-library/jest-dom");

const ConfigProvider = require("antd").ConfigProvider;
const zhCN = require("antd/locale/zh_CN").default;
const DataQualityPage = require("../src/pages/DataQualityPage").default;
const api = require("../src/api/dataQuality");
const dsApi = require("../src/api/datasource");

api.listRules = async () => [{
  id: 1, ruleCode: "RULE_001", ruleName: "订单金额非空",
  ruleType: "COMPLETENESS", targetTable: "T_ORDER", targetColumn: "AMOUNT",
  severity: "HIGH", isEnabled: true, description: null,
  createdBy: null, createdTime: "2026-09-01T00:00:00Z",
  updatedTime: "2026-09-01T00:00:00Z"
}];
dsApi.listDataSources = async () => [];

describe("dump", () => {
  it("dump", async () => {
    render(
      ConfigProvider({ locale: zhCN }, DataQualityPage()),
    );
    await new Promise(r => setTimeout(r, 500));
    const buttons = screen.getAllByRole("button");
    console.log("BUTTONS:", JSON.stringify(buttons.map(b => ({ text: (b.textContent || "").trim(), ariaLabel: b.getAttribute("aria-label") }))));
  });
});