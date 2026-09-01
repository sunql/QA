/** Phase 6.x：EntityAutoComplete 组件单测。
 *
 * 覆盖契约：
 * - 1 字符即触发 searchMappings（用户已要求去掉 2 字符门槛）
 * - q 渲染 enterprise_code + name 命中
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
  enterpriseKey: 10105,
  enterpriseCode: "10105",
  sourceSystem: "ERP",
  sourceCode: "10105",
  name: "ACME 供应商",
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

  it("calls searchMappings for single character query (no 2-char floor)", async () => {
    api.searchMappings.mockResolvedValue([sampleHit]);
    renderAC();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "1" } });
    await waitFor(
      () =>
        expect(api.searchMappings).toHaveBeenCalledWith(
          "1",
          expect.objectContaining({ entityType: undefined, limit: 20 }),
        ),
      { timeout: 1000 },
    );
  });

  it("calls searchMappings after debounce for normal-length query", async () => {
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
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "1" } });
    await waitFor(() =>
      expect(api.searchMappings).toHaveBeenCalledWith(
        "1",
        expect.objectContaining({ entityType: "SUPPLIER" }),
      ),
    );
  });

  it("emits onChange(null) when input is cleared", () => {
    const onChange = vi.fn();
    renderAC({ value: 10105, onChange });
    const input = screen.getByRole("combobox") as HTMLInputElement;
    expect(input.value).toBe("10105");
    fireEvent.change(input, { target: { value: "" } });
    expect(onChange).toHaveBeenCalledWith(null);
  });

  // 注：下拉项点击 + option label 渲染的交互由 headless Playwright 跑真实后端验证
  // （antd AutoComplete 在 jsdom 下 dropdown 渲染 portal 行为不稳定）。
});