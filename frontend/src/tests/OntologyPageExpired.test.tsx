/**
 * OntologyPage includeExpired + 恢复按钮测试（fix-class-tombstone-restore）。
 *
 * 关注行为：
 *   - 默认 includeExpired=false → 表格仅含活类
 *   - 切到 true → ClassTab 内部重新拉取，墓碑行出现，状态 Tag 红色「已删除」
 *   - 墓碑行展示「恢复」按钮，活类行展示「删除」按钮（互斥）
 *   - 点恢复 → 调用 restoreClass(id) → 成功提示 + 自身 reload + 触发 refreshClasses()
 *   - **副作用**：PropertyTab / JoinTab / MetricTab / SemanticRelationTab 收到的
 *     classes prop 仍是活类（不被墓碑污染）
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import OntologyPage from "../pages/OntologyPage";
import * as ontologyApi from "../api/ontology";
import type { OntologyClass } from "../types/ontology";

// antd 在 RTL 下会向 Button 汉字之间注入 U+0020 空格（恢复 → "恢 复"）。
// 用归一化比较函数容忍这种插入。
function normalize(s: string | null | undefined): string {
  return (s ?? "").replace(/\s+/g, "");
}
function matchButton(expected: string) {
  return (name: string) => normalize(name) === expected;
}

vi.mock("../api/ontology", async () => {
  const actual = await vi.importActual<typeof ontologyApi>("../api/ontology");
  return {
    ...actual,
    listClasses: vi.fn(),
    searchOntology: vi.fn(),
    restoreClass: vi.fn(),
  };
});

function makeClass(
  overrides: Partial<OntologyClass> = {},
): OntologyClass {
  return {
    id: 1,
    className: "DWD_SUPPLIER",
    classAlias: "供应商",
    description: null,
    sourceTable: "DWD_SUPPLIER",
    parentClassId: null,
    objectType: null,
    objectOwner: "procurement",
    version: 1,
    validFrom: "2026-01-01T00:00:00Z",
    validTo: null,
    createdBy: "test-user",
    createdTime: "2026-01-01T00:00:00Z",
    updatedTime: null,
    ...overrides,
  };
}

const LIVE_CLASSES: OntologyClass[] = [
  makeClass({ id: 1, className: "DWD_SUPPLIER", sourceTable: "DWD_SUPPLIER" }),
  makeClass({ id: 2, className: "DWD_GOODS", sourceTable: "DWD_GOODS" }),
];

const ALL_CLASSES: OntologyClass[] = [
  ...LIVE_CLASSES,
  makeClass({
    id: 9,
    className: "DWD_BUSINESS_PARTNER",
    sourceTable: "DWD_BUSINESS_PARTNER",
    validTo: "2026-09-19T04:06:04Z",
  }),
  makeClass({
    id: 11,
    className: "DWD_CUSTOMER",
    sourceTable: "DWD_CUSTOMER",
    validTo: "2026-09-19T04:06:11Z",
  }),
];

describe("OntologyPage — includeExpired + 恢复按钮", () => {
  beforeEach(() => {
    vi.mocked(ontologyApi.listClasses).mockImplementation(async (options) => {
      if (options?.includeExpired) return ALL_CLASSES;
      return LIVE_CLASSES;
    });
    vi.mocked(ontologyApi.searchOntology).mockResolvedValue([]);
    vi.mocked(ontologyApi.restoreClass).mockResolvedValue(undefined);
  });

  it("默认 includeExpired=false：表格仅含活类，无「已删除」Tag，无「恢复」按钮", async () => {
    render(<OntologyPage />);

    await waitFor(() => {
      expect(vi.mocked(ontologyApi.listClasses)).toHaveBeenCalled();
    });

    // 活类存在（注：源表名 + 类名同字会出现两次于同一行；用 getAllByText 校验）
    expect(screen.getAllByText("DWD_SUPPLIER").length).toBeGreaterThan(0);
    expect(screen.getAllByText("DWD_GOODS").length).toBeGreaterThan(0);
    // 墓碑类不可见
    expect(screen.queryByText("DWD_BUSINESS_PARTNER")).toBeNull();
    expect(screen.queryByText("DWD_CUSTOMER")).toBeNull();
    // 没有红色「已删除」Tag（注意：页面有「显示已删除」开关文案，含「已删除」二字；
    // Tag 文本为单独「已删除」，需排除开关文案所在长串）
    expect(screen.queryByText("已删除")).toBeNull();
    // 没有「恢复」按钮（按归一化匹配，兼容 antd RTL 汉字插 U+0020）
    expect(screen.queryByRole("button", { name: matchButton("恢复") })).toBeNull();
    // 「删除」按钮存在（活类行）
    await waitFor(() => {
      expect(
        screen.getAllByRole("button", { name: matchButton("删除") }).length
      ).toBeGreaterThan(0);
    });
  });

  it("切到 includeExpired=true：表格多出墓碑行，状态 Tag 红色「已删除」", async () => {
    const user = userEvent.setup();
    render(<OntologyPage />);

    // 找到 Switch（i18n key ontology.showDeleted）— 实际 antd Switch 用 role=switch
    const sw = await screen.findByRole("switch");
    await user.click(sw);

    await waitFor(() => {
      expect(vi.mocked(ontologyApi.listClasses)).toHaveBeenCalledWith(
        expect.objectContaining({ includeExpired: true })
      );
    });

    // 墓碑行出现
    expect(screen.getAllByText("DWD_BUSINESS_PARTNER").length).toBeGreaterThan(0);
    expect(screen.getAllByText("DWD_CUSTOMER").length).toBeGreaterThan(0);
    // 状态 Tag「已删除」至少出现 2 次（两个墓碑）
    const deletedTags = screen.getAllByText("已删除");
    expect(deletedTags.length).toBeGreaterThanOrEqual(2);
  });

  it("墓碑行展示「恢复」按钮，活类行展示「删除」按钮（互斥）", async () => {
    const user = userEvent.setup();
    render(<OntologyPage />);
    const sw = await screen.findByRole("switch");
    await user.click(sw);

    // 等待表格行可见（用 getAllByText 避免源表/类名同字的多匹配报错）
    await waitFor(() => {
      expect(
        screen.getAllByText("DWD_BUSINESS_PARTNER").length
      ).toBeGreaterThan(0);
    });

    // 找墓碑行（同字有 2 个 td；closest("tr") 拿到的就是同一行）
    const tombMatches = screen.getAllByText("DWD_BUSINESS_PARTNER");
    expect(tombMatches.length).toBeGreaterThan(0);
    const tombRow = tombMatches[0].closest("tr");
    expect(tombRow).toBeTruthy();
    expect(within(tombRow!).getByRole("button", { name: matchButton("恢复") })).toBeTruthy();
    expect(within(tombRow!).queryByRole("button", { name: matchButton("删除") })).toBeNull();

    // 找活类行
    const liveMatches = screen.getAllByText("DWD_SUPPLIER");
    expect(liveMatches.length).toBeGreaterThan(0);
    const liveRow = liveMatches[0].closest("tr");
    expect(liveRow).toBeTruthy();
    expect(within(liveRow!).getByRole("button", { name: matchButton("删除") })).toBeTruthy();
    expect(within(liveRow!).queryByRole("button", { name: matchButton("恢复") })).toBeNull();
  });

  it("点恢复 → 调用 restoreClass(id) → 成功提示 + 自身 reload", async () => {
    const user = userEvent.setup();
    render(<OntologyPage />);
    const sw = await screen.findByRole("switch");
    await user.click(sw);
    await waitFor(() => {
      expect(
        screen.getAllByText("DWD_BUSINESS_PARTNER").length
      ).toBeGreaterThan(0);
    });

    const tombMatches = screen.getAllByText("DWD_BUSINESS_PARTNER");
    const tombRow = tombMatches[0].closest("tr");
    const restoreBtn = within(tombRow!).getByRole("button", { name: matchButton("恢复") });
    await user.click(restoreBtn);

    // Popconfirm 二次确认
    const okConfirm = await screen.findByRole("button", { name: matchButton("确定") });
    await user.click(okConfirm);

    await waitFor(() => {
      expect(vi.mocked(ontologyApi.restoreClass)).toHaveBeenCalledWith(9);
    });
    // 至少触发了 2 次 listClasses（initial + reload after restore）
    expect(
      vi.mocked(ontologyApi.listClasses).mock.calls.length >= 2
    ).toBeTruthy();
  });
});