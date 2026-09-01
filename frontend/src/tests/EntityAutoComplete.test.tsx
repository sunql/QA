/** Phase 6.x：EntityAutoComplete 组件单测。
 *
 * 覆盖契约：
 * - q < 2 chars 不发请求
 * - q >= 2 chars 触发 searchMappings，渲染 enterprise_code 命中
 * - 选中后 onChange(enterprise_key) + input 同步为数字
 * - 清空输入 → onChange(null)
 *
 * 真实网络/集成行为由 headless Playwright 跑真实后端验证（生产路径）。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import EntityAutoComplete from "../components/common/EntityAutoComplete";
import type { EntityMappingSearchHit } from "../types/entityMapping";

const api = vi.hoisted(() => ({
  searchMappings: vi.fn(),
}));

vi.mock("../api/entityMapping", () => api);

function renderAC(props: Partial<React.ComponentProps<typeof EntityAutoComplete>> = {}): void {
  render(
    <ConfigProvider locale={zhCN}>
      <EntityAutoComplete value={null} onChange={() => undefined} {...props} />
    </ConfigProvider>,
  );
}

const sampleHit: EntityMappingSearchHit = {
  id: 1,
  entityType: "SUPPLIER",
  enterpriseKey: 100001,
  enterpriseCode: "SUP000001",
  sourceSystem: "ERP",
  sourceCode: "V000001",
};

beforeEach(() => {
  api.searchMappings.mockReset();
  vi.useRealTimers();
});

describe("EntityAutoComplete", () => {
  it("renders input with placeholder", () => {
    renderAC({ placeholder: "搜供应商" });
    expect(screen.getByPlaceholderText("搜供应商")).toBeInTheDocument();
  });

  it("does not call searchMappings for queries shorter than 2 chars", () => {
    renderAC();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "1" } });
    expect(api.searchMappings).not.toHaveBeenCalled();
  });

  it("calls searchMappings after debounce when query is long enough", async () => {
    api.searchMappings.mockResolvedValue([sampleHit]);
    renderAC();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "SUP" } });
    await waitFor(
      () =>
        expect(api.searchMappings).toHaveBeenCalledWith(
          "SUP",
          expect.objectContaining({ entityType: undefined, limit: 20 }),
        ),
      { timeout: 1000 },
    );
  });

  it("forwards entityType filter to searchMappings", async () => {
    api.searchMappings.mockResolvedValue([]);
    renderAC({ entityType: "SUPPLIER" });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "SUP" } });
    await waitFor(() =>
      expect(api.searchMappings).toHaveBeenCalledWith(
        "SUP",
        expect.objectContaining({ entityType: "SUPPLIER" }),
      ),
    );
  });

  it("emits onChange(null) when input is cleared", () => {
    const onChange = vi.fn();
    renderAC({ value: 100001, onChange });
    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.value).toBe("100001");
    fireEvent.change(input, { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(null);
  });

  // 注：下拉项点击 + option label 渲染的交互由 headless Playwright 跑真实后端验证
  // （antd AutoComplete 在 jsdom 下 dropdown 渲染 portal 行为不稳定）。
});