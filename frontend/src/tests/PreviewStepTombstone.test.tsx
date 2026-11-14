/**
 * PreviewStep 类冲突渲染测试（fix-class-tombstone-restore + fix-import-tombstone-name-guard）。
 *
 * 关注行为：
 *   - preview.conflicts 含 type="class_tombstoned" → 顶部渲染 Alert
 *   - Alert 列出源表名 + valid_to 时间
 *   - fix-import-tombstone-name-guard：**占用类名**的行才阻断 ——
 *     createClass 只按 class_name 判重（活类/墓碑一视同仁）⇒ 占名的行**禁勾**
 *     （执行期必拒；且墓碑在「本体管理」页默认查不到 ⇒ 用户两头找不着）；
 *     仅源表相同的行**不禁勾**（导入其实能成功，堵死它就是把本可成功的导入
 *     拦在门外，且提示文案是假的）。
 *   - 阻断判定只看 nameCollision，**不看冲突类型**：活类占名同样必然被 createClass
 *     拒 ⇒ 同样禁勾（否则「注定失败的行」要等执行期才炸）。
 *   - 冲突与行的对应关系必须**精确**：按提议表名/提议类名分别索引，
 *     大小写不同的两张表不得互相牵连（否则会误禁一个本可成功的导入）。
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import PreviewStep from "../components/localImport/PreviewStep";
import { zhCN } from "../i18n/zh-CN";
import { enUS } from "../i18n/en-US";
import type {
  ImportConflict,
  ImportPreviewResponse,
  ProposedClass,
  ProposedJoin,
} from "../types/localImport";

function makeClass(sourceTable: string): ProposedClass {
  return {
    sourceTable,
    // 与生产一致：local_import_service 提议类名恒等于表名（class_name=table_name）
    className: sourceTable,
    classAlias: null,
    description: null,
    isSelected: true,
    properties: [],
  };
}

/** 墓碑冲突夹具。nameCollision 必须显式给出：true=占类名（阻断），false=仅同源表（不阻断）。 */
function makeTombstoneConflict(opts: {
  sourceTable: string;
  existingId: number;
  nameCollision: boolean;
  existingName?: string;
  proposedName?: string;
  validTo?: string | null;
}): ImportConflict {
  return {
    type: "class_tombstoned",
    sourceTable: opts.sourceTable,
    sourceColumn: null,
    existingId: opts.existingId,
    existingName: opts.existingName ?? opts.sourceTable,
    proposedName: opts.proposedName ?? opts.sourceTable,
    existingValidTo: opts.validTo === undefined ? "2026-09-19T04:18:04Z" : opts.validTo,
    nameCollision: opts.nameCollision,
    action: "skip",
  };
}

/** 活类占名冲突夹具（type="class"，existing_valid_to 为空 ⇒ 占用者是活类）。 */
function makeLiveConflict(opts: {
  sourceTable: string;
  existingId: number;
  nameCollision: boolean;
  existingName?: string;
  proposedName?: string;
}): ImportConflict {
  return {
    type: "class",
    sourceTable: opts.sourceTable,
    sourceColumn: null,
    existingId: opts.existingId,
    existingName: opts.existingName ?? opts.sourceTable,
    proposedName: opts.proposedName ?? opts.sourceTable,
    existingValidTo: null,
    nameCollision: opts.nameCollision,
    action: "skip",
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

/** 取表格里某个源表所在行的 checkbox。 */
function rowCheckbox(tableEl: HTMLElement, sourceTable: string): HTMLInputElement {
  const matches = within(tableEl).getAllByText(sourceTable);
  expect(matches.length).toBeGreaterThan(0);
  const row = matches[0].closest("tr");
  expect(row).toBeTruthy();
  const checkbox = row!.querySelector('input[type="checkbox"]') as HTMLInputElement;
  expect(checkbox).toBeTruthy();
  return checkbox;
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
    // 没有「软删除」字样
    expect(screen.queryByText(/软删除/)).toBeNull();
  });

  it("纯墓碑冲突渲染 Alert 并列出源表名 + 软删时间", () => {
    const when = "2026-09-19T04:06:04.111862Z";
    const preview = makePreview({
      classes: ["DWD_BUSINESS_PARTNER", "DWD_CUSTOMER", "DWD_SUPPLIER"],
      conflicts: [
        makeTombstoneConflict({
          sourceTable: "DWD_BUSINESS_PARTNER",
          existingId: 9,
          nameCollision: true,
          validTo: when,
        }),
        makeTombstoneConflict({
          sourceTable: "DWD_CUSTOMER",
          existingId: 11,
          nameCollision: true,
          validTo: "2026-09-19T04:06:11.327257Z",
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    // Alert 出现
    const alert = screen.getByTestId("tombstoned-alert");
    expect(alert).toBeTruthy();
    // Alert 内含两个源表名
    expect(within(alert).getByText("DWD_BUSINESS_PARTNER")).toBeTruthy();
    expect(within(alert).getByText("DWD_CUSTOMER")).toBeTruthy();
    // 含软删时间（toLocaleString 渲染为 "2026/9/19 12:06:04"）
    const dateMatches = within(alert).getAllByText((_, el) => {
      if (!el) return false;
      const t = el.textContent ?? "";
      return t.includes("2026") && t.includes("9/19");
    });
    expect(dateMatches.length).toBeGreaterThan(0);
  });

  it("「全选当前筛选」自动跳过占用类名的墓碑源表", async () => {
    const preview = makePreview({
      classes: ["DWD_BUSINESS_PARTNER", "DWD_SUPPLIER"],
      conflicts: [
        makeTombstoneConflict({
          sourceTable: "DWD_BUSINESS_PARTNER",
          existingId: 9,
          nameCollision: true,
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    // 点「全选当前筛选」→ 提交集合应只含 DWD_SUPPLIER（占名墓碑被跳掉）
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

  it("墓碑占用的行禁用勾选：点了也选不上，也不会进入提交集合", async () => {
    const user = userEvent.setup();
    const preview = makePreview({
      classes: ["DWD_BUSINESS_PARTNER", "DWD_SUPPLIER"],
      conflicts: [
        makeTombstoneConflict({
          sourceTable: "DWD_BUSINESS_PARTNER",
          existingId: 9,
          nameCollision: true,
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    // 找到 DWD_BUSINESS_PARTNER 行（在表格内，而非 Alert 文本里）
    const tableEl = screen.getByTestId("classTable");
    const checkbox = rowCheckbox(tableEl, "DWD_BUSINESS_PARTNER");

    // fix-import-tombstone-name-guard：墓碑占用行的勾选框必须禁用 ——
    // 旧实现允许手动勾选，执行期才炸「类名 X 已存在」，而那个墓碑在
    // 「本体管理」页默认查不到 ⇒ 用户两头找不着。禁勾让死结在预览期就可见。
    expect(checkbox.disabled).toBe(true);
    // 强点也点不上（disabled 控件不激活）
    fireEvent.click(checkbox);
    expect(checkbox.checked).toBe(false);

    // 勾一个正常行 → 提交集合里也不能出现墓碑行
    const supplierCheckbox = rowCheckbox(tableEl, "DWD_SUPPLIER");
    expect(supplierCheckbox.disabled).toBe(false);
    await user.click(supplierCheckbox);

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

  it("仅源表相同（类名未被占）的墓碑不阻断：不禁勾、能进提交集合", async () => {
    // createClass 只按 class_name 判重 ⇒ 占用者类名不同 ⇒ 导入其实能成功。
    // 前端若照样禁勾，就把本可成功的导入彻底堵死，且提示文案是假的。
    const preview = makePreview({
      classes: ["DWD_CUSTOMER", "DWD_SUPPLIER"],
      conflicts: [
        makeTombstoneConflict({
          sourceTable: "DWD_CUSTOMER",
          existingId: 42,
          existingName: "DWD_CUSTOMER_OLD",
          proposedName: "DWD_CUSTOMER",
          nameCollision: false,
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    const tableEl = screen.getByTestId("classTable");
    const customerCheckbox = rowCheckbox(tableEl, "DWD_CUSTOMER");
    expect(customerCheckbox.disabled).toBe(false);

    // 「全选当前筛选」也要把它选进去
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/ }));
    const confirmBtn = screen.getByRole("button", { name: /确认导入/ });
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(onExecute).toHaveBeenCalled());
    const payload = onExecute.mock.calls[0][0];
    const confirmedTables = payload.confirmedClasses.map(
      (c: ProposedClass) => c.sourceTable
    );
    expect(confirmedTables).toContain("DWD_CUSTOMER");
    expect(confirmedTables).toContain("DWD_SUPPLIER");
  });

  it("仅源表相同的墓碑：行内标为非阻断状态，不显示占名标签", () => {
    const preview = makePreview({
      classes: ["DWD_CUSTOMER", "DWD_SUPPLIER"],
      conflicts: [
        makeTombstoneConflict({
          sourceTable: "DWD_CUSTOMER",
          existingId: 42,
          existingName: "DWD_CUSTOMER_OLD",
          proposedName: "DWD_CUSTOMER",
          nameCollision: false,
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    const tableEl = screen.getByTestId("classTable");
    // 非阻断 ⇒ 不显示「已被软删除类占用」的占名标签
    expect(within(tableEl).queryByText("已被软删除类占用")).toBeNull();
    // 但仍提示同源表有软删除类
    expect(within(tableEl).getByText("同源表有软删除类")).toBeTruthy();
  });

  it("占名冲突的 Alert 点出占用类的 id（用户据此知道去恢复哪一行）", () => {
    // ⚠️ 夹具必须**后端可产出**：name_collision=true 的占用者来自按类名命中
    // ⇒ occupant.class_name == 提议类名（不可能是「另一个类名」）。
    // ⇒ 「去恢复谁」的可执行线索是 **id**，不是另一个类名。
    // （旧用例用 existingName≠proposedName 的**不可能组合** ⇒ 分支不可达 ⇒ 假绿。）
    const preview = makePreview({
      classes: ["DWD_CUSTOMER"],
      conflicts: [
        makeTombstoneConflict({
          sourceTable: "DWD_CUSTOMER",
          existingId: 42,
          existingName: "DWD_CUSTOMER",
          proposedName: "DWD_CUSTOMER",
          nameCollision: true,
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    const alert = screen.getByTestId("tombstoned-alert");
    expect(within(alert).getByText("DWD_CUSTOMER")).toBeTruthy();
    // Alert 里必须出现占用者 id（这是「去恢复 id=N」的唯一落地处）
    expect(alert.textContent).toContain("42");
    expect(alert.textContent).toContain("占用类名");
  });

  it("表头「全选」也不把墓碑行选进去", async () => {
    const user = userEvent.setup();
    const preview = makePreview({
      classes: ["DWD_BUSINESS_PARTNER", "DWD_SUPPLIER"],
      conflicts: [
        makeTombstoneConflict({
          sourceTable: "DWD_BUSINESS_PARTNER",
          existingId: 9,
          nameCollision: true,
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    // 表头 checkbox = 第一个 input[type=checkbox]（antd 把 select-all 放在表头）
    const tableEl = screen.getByTestId("classTable");
    const headerCheckbox = tableEl.querySelector(
      'thead input[type="checkbox"]'
    ) as HTMLInputElement;
    expect(headerCheckbox).toBeTruthy();
    await user.click(headerCheckbox);

    const confirmBtn = screen.getByRole("button", { name: /确认导入/ });
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(onExecute).toHaveBeenCalled());
    const payload = onExecute.mock.calls[0][0];
    const confirmedTables = payload.confirmedClasses.map(
      (c: ProposedClass) => c.sourceTable
    );
    // 全选不得绕过禁勾：墓碑行仍在提交集合之外
    expect(confirmedTables).not.toContain("DWD_BUSINESS_PARTNER");
    expect(confirmedTables).toContain("DWD_SUPPLIER");
  });

  it("墓碑行就地标注占用状态（墓碑 id + 软删时间）", () => {
    const preview = makePreview({
      classes: ["DWD_BUSINESS_PARTNER", "DWD_SUPPLIER"],
      conflicts: [
        makeTombstoneConflict({
          sourceTable: "DWD_BUSINESS_PARTNER",
          existingId: 9,
          nameCollision: true,
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    const tableEl = screen.getByTestId("classTable");
    // 墓碑行有状态标签
    const tag = within(tableEl).getByText("已被软删除类占用");
    const tombRow = tag.closest("tr");
    expect(tombRow).toBeTruthy();
    // 行内可见说明含墓碑 id 与软删时间（toLocaleString → 2026/9/19 12:18:04）
    expect(tombRow!.textContent).toContain("9");
    expect(tombRow!.textContent).toContain("2026");
    expect(tombRow!.textContent).toContain("9/19");
    // 正常行不标
    const supplierMatches = within(tableEl).getAllByText("DWD_SUPPLIER");
    const supplierRow = supplierMatches[0].closest("tr")!;
    expect(supplierRow.textContent).not.toContain("已被软删除类占用");
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

describe("PreviewStep — 类名占用的活类/墓碑一视同仁", () => {
  let onExecute: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    onExecute = vi.fn();
  });

  it("活类占名（nameCollision=true）同样禁勾，并说明占用者", async () => {
    // createClass 只按 class_name 判重（活类/墓碑一视同仁）⇒ 活类占名的行
    // 执行期必然 422（类名已存在）⇒ 预览期就该拦下来，而不是等执行失败。
    const preview = makePreview({
      classes: ["ORDERS", "DWD_SUPPLIER"],
      conflicts: [
        makeLiveConflict({
          sourceTable: "ORDERS",
          existingId: 7,
          nameCollision: true,
          existingName: "ORDERS",
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    const tableEl = screen.getByTestId("classTable");
    const checkbox = rowCheckbox(tableEl, "ORDERS");
    expect(checkbox.disabled).toBe(true);

    // 状态列要说清是**活类**占用（不能复用墓碑文案「已被软删除类占用」）
    expect(within(tableEl).getByText("类名已被现有类占用")).toBeTruthy();
    expect(within(tableEl).queryByText("已被软删除类占用")).toBeNull();
    // 行内可见占用者 id
    const tag = within(tableEl).getByText("类名已被现有类占用");
    expect(tag.closest("tr")!.textContent).toContain("7");

    // 「全选当前筛选」也不得把它选进去
    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/ }));
    fireEvent.click(screen.getByRole("button", { name: /确认导入/ }));
    await waitFor(() => expect(onExecute).toHaveBeenCalled());
    const payload = onExecute.mock.calls[0][0];
    const confirmedTables = payload.confirmedClasses.map(
      (c: ProposedClass) => c.sourceTable
    );
    expect(confirmedTables).not.toContain("ORDERS");
    expect(confirmedTables).toContain("DWD_SUPPLIER");
  });

  it("活类占名的行出现在顶部汇总 Alert 里", () => {
    const preview = makePreview({
      classes: ["ORDERS", "DWD_SUPPLIER"],
      conflicts: [
        makeLiveConflict({
          sourceTable: "ORDERS",
          existingId: 7,
          nameCollision: true,
          existingName: "ORDERS",
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    const alert = screen.getByTestId("name-taken-alert");
    expect(within(alert).getByText("ORDERS")).toBeTruthy();
    expect(alert.textContent).toContain("7");
  });

  it("大小写不同的同源表不被误禁勾：只有真正占名的那一行被禁", async () => {
    // 后端口径：类名**大小写敏感**（对齐 PG `=`），源表**大小写不敏感**。
    // ⇒ 墓碑 "T1" 对提议类名 "t1" 只是撞表（nameCollision=false，导入能成功）。
    // 前端若把表名/类名混在一个小写 key 空间里"先到先得"，
    // "t1" 就会被 "T1" 的占名冲突牵连 ⇒ 本可成功的导入被堵死。
    const preview = makePreview({
      classes: ["T1", "t1"],
      conflicts: [
        makeTombstoneConflict({
          sourceTable: "T1",
          existingId: 5,
          existingName: "T1",
          proposedName: "T1",
          nameCollision: true,
        }),
        makeTombstoneConflict({
          sourceTable: "t1",
          existingId: 5,
          existingName: "T1",
          proposedName: "t1",
          nameCollision: false,
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    const tableEl = screen.getByTestId("classTable");
    expect(rowCheckbox(tableEl, "T1").disabled).toBe(true);
    expect(rowCheckbox(tableEl, "t1").disabled).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: /全选当前筛选/ }));
    fireEvent.click(screen.getByRole("button", { name: /确认导入/ }));
    await waitFor(() => expect(onExecute).toHaveBeenCalled());
    const payload = onExecute.mock.calls[0][0];
    const confirmedTables = payload.confirmedClasses.map(
      (c: ProposedClass) => c.sourceTable
    );
    expect(confirmedTables).not.toContain("T1");
    expect(confirmedTables).toContain("t1");
  });

  it("后端未提供 nameCollision 时不承诺「可正常导入」", () => {
    // 灰度期（前端已上、后端未上）：nameCollision 缺失 ⇒ 占名与否**未知**。
    // 这时若照旧渲染「不影响本次导入」，就是给出一个假保证（执行期仍可能失败）。
    const preview = makePreview({
      classes: ["DWD_CUSTOMER"],
      conflicts: [
        {
          type: "class_tombstoned",
          sourceTable: "DWD_CUSTOMER",
          sourceColumn: null,
          existingId: 42,
          existingName: "DWD_CUSTOMER",
          proposedName: "DWD_CUSTOMER",
          existingValidTo: "2026-09-19T04:06:04Z",
          action: "skip",
        },
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    const alert = screen.getByTestId("tombstoned-alert");
    expect(alert.textContent).not.toContain("可正常导入");
    expect(alert.textContent).toContain("执行期确认");
  });

  it("仅撞表的冲突不说「占用类名」（类名根本没被占）", () => {
    const preview = makePreview({
      classes: ["DWD_CUSTOMER"],
      conflicts: [
        makeTombstoneConflict({
          sourceTable: "DWD_CUSTOMER",
          existingId: 42,
          existingName: "DWD_CUSTOMER_OLD",
          proposedName: "DWD_CUSTOMER",
          nameCollision: false,
        }),
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    const alert = screen.getByTestId("tombstoned-alert");
    // 「占用类名 X」与「类名未被占用」自相矛盾 ⇒ 只有真占名才说「占用类名」
    expect(alert.textContent).not.toContain("占用类名");
    expect(alert.textContent).toContain("仅源表相同");
  });

  it("活类冲突在后端未判定时给中性提示，而不是「—」装死", () => {
    // 灰度期：活类冲突没有 nameCollision ⇒ 占名与否未知。
    // ⇒ 行内不能是「—」（那一行可能在执行期 422，用户却什么都看不到），
    //    也不能说「类名已被占用」（没判定）⇒ 中性提示。
    const preview = makePreview({
      classes: ["DWD_CUSTOMER"],
      conflicts: [
        {
          type: "class",
          sourceTable: "DWD_CUSTOMER",
          sourceColumn: null,
          existingId: 42,
          existingName: "DWD_CUSTOMER",
          proposedName: "DWD_CUSTOMER",
          existingValidTo: null,
          action: "skip",
        },
      ],
    });
    render(<PreviewStep preview={preview} onExecute={onExecute} />);

    const tableEl = screen.getByTestId("classTable");
    expect(within(tableEl).getByText("与现有类同名，待确认")).toBeTruthy();
    // 未判定 ⇒ 不断言占名
    expect(within(tableEl).queryByText("类名已被现有类占用")).toBeNull();
  });
});

/** 收集对象下的所有叶子键（点分路径）。 */
function leafKeys(value: unknown, prefix = ""): string[] {
  if (!value || typeof value !== "object") return [];
  return Object.entries(value as Record<string, unknown>).flatMap(([k, v]) =>
    leafKeys(v, prefix ? `${prefix}.${k}` : k),
  );
}

describe("PreviewStep 文案：zh-CN / en-US 键集对等", () => {
  it("localImport.preview 两侧键集完全一致（漏翻即红）", () => {
    const zh = leafKeys((zhCN as any).localImport?.preview).sort();
    const en = leafKeys((enUS as any).localImport?.preview).sort();
    expect(zh).toEqual(en);
  });
});
