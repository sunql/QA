/** 场景高级参数槽位控件：无远程搜索映射的 entityMulti（如 orderKeys）
 * 必须退化为自由文本标签输入（旧实现 options=[] + filterOption=false ⇒ 死控件）；
 * 有映射的 supplierKeys 必须仍是远程搜索多选（回归守卫）。
 */

import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
        isAdmin={false}
        datasources={[]}
        datasourceId={undefined}
        onDatasourceChange={() => undefined}
        siteOptions={[]}
      />
      {/* 槽位受控值只从 onChange 流转 ⇒ 用输出节点精确断言（antd 在 jsdom
          下 maxTagCount="responsive" 会收起标签，DOM 标签文本不可靠）。 */}
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

/** 面板内 Select 顺序：模型（0）在前，槽位从 1 开始。 */
function slotSelect(index: number): HTMLElement {
  return screen.getAllByRole("combobox")[index];
}

function slotValues(): SlotValues {
  return JSON.parse(screen.getByTestId("slot-values").textContent ?? "{}");
}

const ORDER_SLOT: CopilotScenarioSlot = {
  key: "orderKeys",
  type: "entityMulti",
  label: "订单",
};

const SUPPLIER_SLOT: CopilotScenarioSlot = {
  key: "supplierKeys",
  type: "entityMulti",
  label: "供应商",
};

describe("ScenarioAdvancedParams orderKeys（自由文本标签）", () => {
  it("可输入订单号并回车成标签（不再是死控件）", async () => {
    const user = userEvent.setup();
    renderParams([ORDER_SLOT]);

    const input = slotSelect(1);
    await user.click(input);
    await user.type(input, "T22611POH0223");
    // userEvent 的 {Enter} 不带 which/keyCode，rc-select 只认 event.which ⇒
    // 用 fireEvent 补全键码（等价于真实回车提交当前输入为标签）
    fireEvent.keyDown(input, { key: "Enter", keyCode: 13, which: 13 });

    await waitFor(() =>
      expect(slotValues().orderKeys).toEqual(["T22611POH0223"]),
    );
    // 订单号没有远程映射：不应触发实体搜索
    expect(api.searchMappings).not.toHaveBeenCalled();
  });

  it("支持逗号/空格分隔一次输入多个订单号", async () => {
    const user = userEvent.setup();
    renderParams([ORDER_SLOT]);

    const input = slotSelect(1);
    await user.click(input);
    // 逗号分隔即时成标签；最后一个回车提交（键码同上）
    await user.type(input, "PO-AAA,PO-BBB");
    fireEvent.keyDown(input, { key: "Enter", keyCode: 13, which: 13 });

    await waitFor(() =>
      expect(slotValues().orderKeys).toEqual(["PO-AAA", "PO-BBB"]),
    );
  });
});

describe("ScenarioAdvancedParams supplierKeys（远程搜索回归守卫）", () => {
  it("输入关键字触发远程搜索，命中的供应商可被选中", async () => {
    api.searchMappings.mockResolvedValue([
      {
        id: 1,
        entityType: "SUPPLIER",
        enterpriseKey: 1,
        enterpriseCode: "F109",
        sourceSystem: "ERP",
        sourceCode: "ERP-1",
        name: "飞驰机械",
      },
    ]);
    const user = userEvent.setup();
    renderParams([SUPPLIER_SLOT]);

    await user.type(slotSelect(1), "飞驰");

    await waitFor(() =>
      expect(api.searchMappings).toHaveBeenCalledWith("飞驰", {
        entityType: "SUPPLIER",
        limit: 20,
      }),
    );
    await user.click(await screen.findByText("飞驰机械（F109）"));

    await waitFor(() =>
      expect(slotValues().supplierKeys).toEqual(["F109"]),
    );
  });
});
