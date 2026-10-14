/**
 * PreviewStep 墓碑冲突渲染测试（fix-class-tombstone-restore）。
 *
 * 关注行为：
 *   - preview.conflicts 含 type="class_tombstoned" → 顶部渲染 Alert
 *   - Alert 列出源表名 + valid_to 时间
 *   - 初次 mount 自动剔除墓碑源表（不进默认选中集）
 *   - 用户手动勾选墓碑源表 → 仍允许（与文案「确认忽略」一致，不强禁）
 *   - 无墓碑冲突 → 不渲染 Alert
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import PreviewStep from "../components/localImport/PreviewStep";
import type {
  ImportConflict,
  ImportPreviewResponse,
  ProposedClass,
  ProposedJoin,
} from "../types/localImport";

function makeClass(sourceTable: string): ProposedClass {
  return {
    sourceTable,
    className: sourceTable.toUpperCase(),
    classAlias: null,
    description: null,
    isSelected: true,
    properties: [],
  };
}

function makePreview(opts: {
  classes: string[];
  conflicts?: ImportConflict[];
  joins?: ProposedJoin[];
}): ImportPreviewResponse {
  return {
    datasourceId: 1,
    proposedClasses: opts.classes.map(makeClass),
    proposedJoins: opts.joins ?? [],
    conflicts: opts.conflicts ?? [],
    filterSuggestions: { recommendedBlacklistPatterns: [], excludedTables: [] },
    llmUsage: { modelName: null, promptTokens: 0, completionTokens: 0 },
  };
}

describe("PreviewStep — 墓碑冲突（class_tombstoned）", () => {
  let onExecute: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    onExecute = vi.fn();
  });

  it("无冲突时不渲染 Alert", () => {
    const preview = makePreview({
      classes: ["DWD_SUPPLIER", "DWD_GOODS"],
      conflicts: [],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);
    // 没有「已被同名软删除」字样
    expect(screen.queryByText(/软删除/)).toBeNull();
  });

  it("纯墓碑冲突渲染 Alert 并列出源表名 + 软删时间", () => {
    const when = "2026-09-19T04:06:04.111862Z";
    const preview = makePreview({
      classes: ["DWD_BUSINESS_PARTNER", "DWD_CUSTOMER", "DWD_SUPPLIER"],
      conflicts: [
        {
          type: "class_tombstoned",
          sourceTable: "DWD_BUSINESS_PARTNER",
          sourceColumn: null,
          existingId: 9,
          existingName: "DWD_BUSINESS_PARTNER",
          proposedName: "DWD_BUSINESS_PARTNER",
          existingValidTo: when,
          action: "skip",
        },
        {
          type: "class_tombstoned",
          sourceTable: "DWD_CUSTOMER",
          sourceColumn: null,
          existingId: 11,
          existingName: "DWD_CUSTOMER",
          proposedName: "DWD_CUSTOMER",
          existingValidTo: "2026-09-19T04:06:11.327257Z",
          action: "skip",
        },
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    // Alert 出现
    const alert = screen.getByTestId("tombstoned-alert");
    expect(alert).toBeTruthy();
    // Alert 内含两个源表名
    expect(within(alert).getByText("DWD_BUSINESS_PARTNER")).toBeTruthy();
    expect(within(alert).getByText("DWD_CUSTOMER")).toBeTruthy();
    // 含软删时间（toLocaleString("zh-CN") 渲染为 "2026/9/19 12:06:04"）
    const dateMatches = within(alert).getAllByText((_, el) => {
      if (!el) return false;
      const t = el.textContent ?? "";
      return t.includes("2026") && t.includes("9/19");
    });
    expect(dateMatches.length).toBeGreaterThan(0);
  });

  it("「全选当前筛选」自动跳过墓碑源表", async () => {
    const preview = makePreview({
      classes: ["DWD_BUSINESS_PARTNER", "DWD_SUPPLIER"],
      conflicts: [
        {
          type: "class_tombstoned",
          sourceTable: "DWD_BUSINESS_PARTNER",
          sourceColumn: null,
          existingId: 9,
          existingName: "DWD_BUSINESS_PARTNER",
          proposedName: "DWD_BUSINESS_PARTNER",
          existingValidTo: "2026-09-19T04:06:04Z",
          action: "skip",
        },
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    // 点「全选当前筛选」→ 提交集合应只含 DWD_SUPPLIER（墓碑被跳掉）
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/ }));

    const confirmBtn = screen.getByRole("button", { name: /确认导入/ });
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(onExecute).toHaveBeenCalled());
    const payload = onExecute.mock.calls[0][0];
    const confirmedTables = payload.confirmedClasses.map(
      (c: ProposedClass) => c.sourceTable
    );
    expect(confirmedTables).toEqual(["DWD_SUPPLIER"]);
    expect(confirmedTables).not.toContain("DWD_BUSINESS_PARTNER");
  });

  it("用户手动勾选墓碑源表后允许包含在最终提交中（不强禁）", async () => {
    const user = userEvent.setup();
    const preview = makePreview({
      classes: ["DWD_BUSINESS_PARTNER", "DWD_SUPPLIER"],
      conflicts: [
        {
          type: "class_tombstoned",
          sourceTable: "DWD_BUSINESS_PARTNER",
          sourceColumn: null,
          existingId: 9,
          existingName: "DWD_BUSINESS_PARTNER",
          proposedName: "DWD_BUSINESS_PARTNER",
          existingValidTo: "2026-09-19T04:18:04Z",
          action: "skip",
        },
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    // 找到 DWD_BUSINESS_PARTNER 行（在表格内，而非 Alert 文本里）
    // 注：源表列 + 类名列文字相同（ProposedClass.className 由 sourceTable 大写化），
    // 所以会有两个 <td>；closest("tr") 拿到的就是同一行。
    const tableEl = screen.getByTestId("classTable");
    const matches = within(tableEl).getAllByText("DWD_BUSINESS_PARTNER");
    expect(matches.length).toBeGreaterThan(0);
    const tombRow = matches[0].closest("tr");
    expect(tombRow).toBeTruthy();
    const checkbox = tombRow!.querySelector(
      'input[type="checkbox"]'
    ) as HTMLInputElement;
    await user.click(checkbox);

    const confirmBtn = screen.getByRole("button", { name: /确认导入/ });
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(onExecute).toHaveBeenCalled());
    const payload = onExecute.mock.calls[0][0];
    const confirmedTables = payload.confirmedClasses.map(
      (c: ProposedClass) => c.sourceTable
    );
    // 用户主动勾了 → 进入提交集合（执行期由后端 createClass 拦截；不是前端职责）
    expect(confirmedTables).toContain("DWD_BUSINESS_PARTNER");
  });

  it("CLASS 冲突（非墓碑）不渲染墓碑 Alert", () => {
    const preview = makePreview({
      classes: ["DWD_SUPPLIER", "DWD_GOODS"],
      conflicts: [
        {
          type: "class",
          sourceTable: "DWD_SUPPLIER",
          sourceColumn: null,
          existingId: 1,
          existingName: "DWD_SUPPLIER",
          proposedName: "DWD_SUPPLIER",
          existingValidTo: null,
          action: "skip",
        },
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);
    expect(screen.queryByText(/软删除/i)).toBeNull();
  });
});