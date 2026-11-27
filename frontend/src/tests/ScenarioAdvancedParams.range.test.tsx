/** number 槽位的取值范围约束（2026-11-28 生产缺陷：周期期序填 1 ⇒ 查了所有供应商）。
 *
 * - InputNumber 按场景声明的 minimum / maximum 限制（aria-valuemin / aria-valuemax）；
 * - 用户输入越界 ⇒ 就地提示，且**越界值不进槽位值**（不会发给后端）。
 */

import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { I18nextProvider } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import i18n from "../i18n/i18n";
import type { CopilotScenarioSlot } from "../api/copilotScenario";
import ScenarioAdvancedParams, {
  type SlotValues,
} from "../components/copilot/ScenarioAdvancedParams";

const api = vi.hoisted(() => ({ searchMappings: vi.fn() }));

vi.mock("../api/entityMapping", () => ({ searchMappings: api.searchMappings }));

beforeEach(() => {
  api.searchMappings.mockReset();
  api.searchMappings.mockResolvedValue([]);
});

function Harness({ slots }: { slots: CopilotScenarioSlot[] }) {
  const [values, setValues] = useState<SlotValues>({});
  return (
    <>
      <ScenarioAdvancedParams
        slots={slots}
        values={values}
        onChange={(key, value) => setValues((prev) => ({ ...prev, [key]: value }))}
        dateMode="months"
        onDateModeChange={() => undefined}
        models={[]}
        modelId={undefined}
        onModelChange={() => undefined}
        siteOptions={[]}
      />
      <output data-testid="slot-values">{JSON.stringify(values)}</output>
    </>
  );
}

function renderParams(slots: CopilotScenarioSlot[]) {
  return render(
    <MemoryRouter>
      <I18nextProvider i18n={i18n}>
        <ConfigProvider locale={zhCN}>
          <Harness slots={slots} />
        </ConfigProvider>
      </I18nextProvider>
    </MemoryRouter>,
  );
}

function slotValues(): SlotValues {
  return JSON.parse(screen.getByTestId("slot-values").textContent ?? "{}");
}

/** 到货跟踪场景的「周期期序」槽位（0=本期、-1=上期，正数非法）。 */
const PERIOD_INDEX_SLOT: CopilotScenarioSlot = {
  key: "periodIndex",
  type: "number",
  label: "周期期序",
  minimum: -12,
  maximum: 0,
};

/** 未声明范围的 number 槽位。 */
const PLAIN_NUMBER_SLOT: CopilotScenarioSlot = {
  key: "someCount",
  type: "number",
  label: "某个计数",
};

describe("ScenarioAdvancedParams number 槽位范围", () => {
  it("按场景声明的限制取值范围（aria-valuemin / aria-valuemax）", () => {
    renderParams([PERIOD_INDEX_SLOT]);

    const input = screen.getByRole("spinbutton");
    expect(input.getAttribute("aria-valuemin")).toBe("-12");
    expect(input.getAttribute("aria-valuemax")).toBe("0");
  });

  it("未声明范围时不加范围限制", () => {
    renderParams([PLAIN_NUMBER_SLOT]);

    const input = screen.getByRole("spinbutton");
    expect(input.getAttribute("aria-valuemin")).toBeNull();
    expect(input.getAttribute("aria-valuemax")).toBeNull();
  });

  it("输入越界值时给出明确提示", async () => {
    const user = userEvent.setup();
    renderParams([PERIOD_INDEX_SLOT]);

    const input = screen.getByRole("spinbutton");
    await user.type(input, "1");

    const hint = await screen.findByText(/必须在/);
    expect(hint.textContent).toContain("周期期序");
    expect(hint.textContent).toContain("-12");
  });

  it("越界值不进槽位值（不会发给后端）", async () => {
    const user = userEvent.setup();
    renderParams([PERIOD_INDEX_SLOT]);

    const input = screen.getByRole("spinbutton");
    await user.type(input, "1");

    await waitFor(() => {
      expect(screen.getByText(/必须在/)).toBeTruthy();
    });
    expect(slotValues().periodIndex).toBeUndefined();
  });

  it("范围内的值正常进槽位值", async () => {
    const user = userEvent.setup();
    renderParams([PERIOD_INDEX_SLOT]);

    const input = screen.getByRole("spinbutton");
    await user.clear(input);
    await user.type(input, "-1");

    await waitFor(() => {
      expect(slotValues().periodIndex).toBe(-1);
    });
  });
});
