/** feat-violation-sample-display (2026-09-15) 单元测试：
 *  验证 ViolationSampleTable 渲染 samplePkValues 字段，pre-5 + Tooltip + 「+N more」。
 *
 *  历史 bug：原来只渲染元信息列（ruleId/targetTable/totalViolations/sampleSize/capturedAt），
 *  samplePkValues JSONB 字段完全没展示——用户看到 sampleSize=20 但不知道是哪 20 条。
 *
 *  关注点：
 *    1. < 5 个 PK：直接渲染 N 个红色 Tag，无 Tooltip 文字
 *    2. = 5 个 PK：5 个 Tag + Tooltip 含完整列表（同一组渲染，可 hover 看全）
 *    3. > 5 个 PK：前 5 个 Tag + 「+N more」文字（用 i18n moreSamples key）
 *    4. 0 个 PK：显示「—」占位
 *    5. 异常 schema（entry 无 pk 字段 / pk 是 null）：容错回退「—」
 *    6. samples=[]：渲染 Empty
 */

import { describe, it, expect } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import ViolationSampleTable from "../components/ViolationSampleTable";
import type { ViolationSampleRead } from "../types/evaluationReport";

function makeSample(overrides: Partial<ViolationSampleRead> = {}): ViolationSampleRead {
  return {
    id: 1,
    reportId: 100,
    ruleId: 42,
    datasourceId: 7,
    targetTable: "BPSUPPLIER",
    targetColumn: "ITF_ID",
    totalViolations: 1234,
    sampleSize: 5,
    samplePkValues: [],
    capturedAt: "2026-09-15T03:21:00Z",
    ...overrides,
  };
}

function renderTable(props: {
  samples: ViolationSampleRead[];
  loading?: boolean;
}) {
  return render(
    <ConfigProvider locale={zhCN}>
      <ViolationSampleTable {...props} />
    </ConfigProvider>,
  );
}

describe("ViolationSampleTable — samplePkValues 列 (feat-violation-sample-display)", () => {
  it("样本数 ≤ 5 时直接列出全部 Tag，无 Tooltip 触发文字", () => {
    const samples = [
      makeSample({
        id: 1,
        sampleSize: 3,
        samplePkValues: [{ pk: "A001" }, { pk: "A002" }, { pk: "A003" }],
      }),
    ];
    renderTable({ samples });

    // 3 个 PK 全部可见
    expect(screen.getByText("A001")).toBeInTheDocument();
    expect(screen.getByText("A002")).toBeInTheDocument();
    expect(screen.getByText("A003")).toBeInTheDocument();

    // 没有「+N more」
    expect(screen.queryByText(/more/i)).not.toBeInTheDocument();
  });

  it("样本数 > 5 时显示前 5 个 Tag + 「+N more」提示", () => {
    const pks = [
      { pk: "P001" },
      { pk: "P002" },
      { pk: "P003" },
      { pk: "P004" },
      { pk: "P005" },
      { pk: "P006" },
      { pk: "P007" },
    ];
    const samples = [makeSample({ id: 2, sampleSize: 7, samplePkValues: pks })];
    renderTable({ samples });

    // 前 5 个可见
    expect(screen.getByText("P001")).toBeInTheDocument();
    expect(screen.getByText("P005")).toBeInTheDocument();

    // P006 / P007 不在主视图中（要 hover Tooltip 才看到）
    expect(screen.queryByText("P006")).not.toBeInTheDocument();
    expect(screen.queryByText("P007")).not.toBeInTheDocument();

    // 「+2 more」使用 zh-CN i18n 文案「+{count} 更多」
    expect(screen.getByText("+2 更多")).toBeInTheDocument();
  });

  it("样本数恰好 5 时不显示「+N more」（more=0 走另一条分支）", () => {
    const pks = [
      { pk: "Q001" },
      { pk: "Q002" },
      { pk: "Q003" },
      { pk: "Q004" },
      { pk: "Q005" },
    ];
    const samples = [makeSample({ id: 3, sampleSize: 5, samplePkValues: pks })];
    renderTable({ samples });

    expect(screen.getByText("Q001")).toBeInTheDocument();
    expect(screen.queryByText(/more/i)).not.toBeInTheDocument();
  });

  it("空数组显示「—」占位 Tag，不渲染任何红色 PK Tag", () => {
    const samples = [makeSample({ id: 4, sampleSize: 0, samplePkValues: [] })];
    renderTable({ samples });

    // 表头仍存在
    expect(screen.getByText("违规样例 PK")).toBeInTheDocument();
    // 至少有一个「—」占位（targetColumn 和 samplePkValues 两列都可能显示）
    const placeholders = screen.getAllByText("—");
    expect(placeholders.length).toBeGreaterThanOrEqual(1);
  });

  it("异常 schema：entry 不是 {pk: ...} 时降级为「—」", () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const malformed = [
      { foo: "bar" },
      null,
      { pk: null },
      { pk: "" },
      { pk: "VALID" },
    ] as any;
    const samples = [
      makeSample({ id: 5, sampleSize: 5, samplePkValues: malformed }),
    ];
    renderTable({ samples });

    // 唯一有效 PK 渲染
    expect(screen.getByText("VALID")).toBeInTheDocument();
    // 至少有 1 个「—」（来自 null / null pk / "" pk / foo=bar）
    const placeholders = screen.getAllByText("—");
    expect(placeholders.length).toBeGreaterThanOrEqual(4);
  });

  it("samples 为空数组时显示 Empty 而非表格", () => {
    renderTable({ samples: [] });
    expect(screen.getByText("暂无违规样本")).toBeInTheDocument();
    // 表头不应渲染
    expect(screen.queryByText("违规样例 PK")).not.toBeInTheDocument();
  });

  it("多行：每行的 samplePkValues 各自渲染", () => {
    const samples = [
      makeSample({
        id: 10,
        sampleSize: 2,
        samplePkValues: [{ pk: "X1" }, { pk: "X2" }],
      }),
      makeSample({
        id: 11,
        sampleSize: 8,
        samplePkValues: [
          { pk: "Y1" },
          { pk: "Y2" },
          { pk: "Y3" },
          { pk: "Y4" },
          { pk: "Y5" },
          { pk: "Y6" },
          { pk: "Y7" },
          { pk: "Y8" },
        ],
      }),
    ];
    renderTable({ samples });

    // 第 1 行：2 个全显
    expect(screen.getByText("X1")).toBeInTheDocument();
    expect(screen.getByText("X2")).toBeInTheDocument();
    // 第 2 行：前 5 显，剩下进 Tooltip
    expect(screen.getByText("Y1")).toBeInTheDocument();
    expect(screen.getByText("Y5")).toBeInTheDocument();
    expect(screen.queryByText("Y6")).not.toBeInTheDocument();
    // 「+3 更多」
    expect(screen.getByText("+3 更多")).toBeInTheDocument();
  });

  it("Tooltip 内含完整 PK 列表（hover 触发）", async () => {
    const user = userEvent.setup();
    const pks = [
      { pk: "T001" },
      { pk: "T002" },
      { pk: "T003" },
      { pk: "T004" },
      { pk: "T005" },
      { pk: "T006" },
    ];
    const samples = [makeSample({ id: 20, sampleSize: 6, samplePkValues: pks })];
    renderTable({ samples });

    // 「+1 更多」文字是 Tooltip 触发器
    const more = screen.getByText("+1 更多");
    await user.hover(more);

    await waitFor(() => {
      // react-i18next + antd Tooltip:title 直接渲染 title 文本（含逗号分隔）
      expect(
        screen.getByText("T001, T002, T003, T004, T005, T006"),
      ).toBeInTheDocument();
    });
  });

  // feat-sampling-error-visible (2026-09-15)：采样失败行渲染优先级
  it("samplingError 非空时显示「采样失败」红色 Tag，忽略 samplePkValues", () => {
    const samples = [
      makeSample({
        id: 30,
        sampleSize: 0,
        samplePkValues: [],
        samplingError: "ORA-00933: SQL command not properly ended",
      }),
    ];
    renderTable({ samples });

    // 显示「采样失败」tag
    expect(screen.getByText("采样失败")).toBeInTheDocument();
    // sampleSize=0 但不显示 PK Tag
    expect(screen.queryByText("T001")).not.toBeInTheDocument();
  });

  it("samplingError 文本会作为 Tooltip title 暴露（hover 显示）", async () => {
    const user = userEvent.setup();
    const samples = [
      makeSample({
        id: 31,
        sampleSize: 0,
        samplePkValues: [],
        samplingError: "ORA-00904: BPSUPPLIER.INVALID_COL invalid identifier",
      }),
    ];
    renderTable({ samples });

    const tag = screen.getByText("采样失败");
    await user.hover(tag);

    await waitFor(() => {
      // 完整 ORA 文本应出现在 Tooltip title 内
      expect(
        screen.getByText(
          "ORA-00904: BPSUPPLIER.INVALID_COL invalid identifier",
        ),
      ).toBeInTheDocument();
    });
  });

  it("samplingError 为 null/undefined 时走正常 PK 渲染路径", () => {
    const samples = [
      makeSample({
        id: 32,
        sampleSize: 1,
        samplePkValues: [{ pk: "OK1" }],
        samplingError: null,
      }),
      makeSample({
        id: 33,
        sampleSize: 1,
        samplePkValues: [{ pk: "OK2" }],
        // samplingError undefined（API 不返回字段）
      }),
    ];
    renderTable({ samples });

    // 不显示「采样失败」
    expect(screen.queryByText("采样失败")).not.toBeInTheDocument();
    // 正常 PK 渲染
    expect(screen.getByText("OK1")).toBeInTheDocument();
    expect(screen.getByText("OK2")).toBeInTheDocument();
  });
});
