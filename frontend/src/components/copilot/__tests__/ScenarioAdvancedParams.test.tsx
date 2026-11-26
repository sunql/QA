/** hidden 槽位契约：`hidden: true` 的槽位不得渲染。

后端 `slot_schema` 里 dateFrom/dateTo 是 hidden（由时间窗区间控件写入）；
一旦 API 丢了 hidden 字段，这里就会出现在高级参数面板里变成可见输入框。
 */

import { render, screen } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { describe, expect, it, vi } from "vitest";
import i18n from "../../../i18n/i18n";
import type { CopilotScenarioSlot } from "../../../api/copilotScenario";
import ScenarioAdvancedParams from "../ScenarioAdvancedParams";

vi.mock("../../../api/entityMapping", () => ({ searchMappings: vi.fn() }));

const SLOTS: CopilotScenarioSlot[] = [
  { key: "monthsBack", type: "number", label: "时间窗" },
  { key: "dateFrom", type: "text", label: "开始日期", hidden: true },
  { key: "dateTo", type: "text", label: "结束日期", hidden: true },
];

function renderPanel() {
  return render(
    <I18nextProvider i18n={i18n}>
      <ScenarioAdvancedParams
        slots={SLOTS}
        values={{}}
        onChange={() => undefined}
        dateMode="months"
        onDateModeChange={() => undefined}
        models={[]}
        modelId={undefined}
        onModelChange={() => undefined}
        siteOptions={[]}
      />
    </I18nextProvider>,
  );
}

describe("ScenarioAdvancedParams hidden 槽位", () => {
  it("hidden: true 的槽位不渲染，可见槽位照常渲染", () => {
    renderPanel();
    expect(screen.getByText("时间窗")).toBeTruthy();
    expect(screen.queryByText("开始日期")).toBeNull();
    expect(screen.queryByText("结束日期")).toBeNull();
  });
});

describe("ScenarioAdvancedParams 数据源下拉已移除", () => {
  it("不再渲染「数据源」标签与下拉（THBI 硬规则，admin 也不再可见）", () => {
    renderPanel();
    expect(screen.queryByText("数据源")).toBeNull();
    expect(screen.queryByText("THBI Oracle")).toBeNull();
  });
});
