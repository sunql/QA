/**
 * BulkImportModal tests（frontend/src/components/entity-mapping/BulkImportModal.tsx）。
 *
 * 断言口径：**行为**，不是"渲染没崩"。
 *  - 解析 → 预览表的 OK / FAIL 标签与逐行失败原因（前端纯校验，不打后端）
 *  - 导入 → bulkImportMappings 收到的 payload 逐字段核对（EntityMappingCreate 契约）
 *  - 结果 → 结果表渲染 + onSuccess **仅**在 failed===0 时触发（配反向用例）
 *  - 上传 / 下载 → csvDownload 替身收到的真实入参
 *
 * 反假绿约束：
 *  - "没调用 API" 的断言必须配一条"被调用"的正向用例
 *  - payload 一律断言**提交出去的对象**，不读表单内部 state
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import BulkImportModal from "../components/entity-mapping/BulkImportModal";
import type { EntityMappingBulkResult } from "../types/entityMapping";

// =============================================================================
// Mocks
// =============================================================================

const api = vi.hoisted(() => ({
  bulkImportMappings: vi.fn(),
}));
vi.mock("../api/entityMapping", () => api);

// utils/csvDownload 在 jsdom 里不可用（URL.createObjectURL 未实现）→ 替身；
// 签名与真实模块一致：downloadCsv(csv, filename, options?) / readFileAsText(file, opts?)
const csvDownload = vi.hoisted(() => ({
  downloadCsv: vi.fn(),
  readFileAsText: vi.fn(),
}));
vi.mock("../utils/csvDownload", () => csvDownload);

// antd 静态 message 会往 document.body 的 portal 里塞 toast，跨用例残留会让
// getByText 撞到上一个用例的提示 → 改成替身断言（与 ChangePasswordPage 同口径）。
const messageMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  return {
    ...actual,
    message: {
      ...(actual.message ?? {}),
      success: messageMock.success,
      error: messageMock.error,
      warning: messageMock.warning,
      info: messageMock.info,
    },
  };
});

// =============================================================================
// Fixtures
// =============================================================================

const CSV_HEADER =
  "entity_type,enterprise_code,source_system,source_code,match_rule,name,effective_date,expiry_date";

/** 1 行完全合法。 */
const VALID_CSV = [
  CSV_HEADER,
  "SUPPLIER,SUP000001,SRM,SRM-2024-001,MDM_MASTER,北京XX有限公司,2026-01-01,",
].join("\n");

/** 1 行合法 + 1 行 entity_type 越界 → 前端校验拦下，不进 payload。 */
const MIXED_CSV = [
  CSV_HEADER,
  "SUPPLIER,SUP000001,SRM,SRM-2024-001,MDM_MASTER,北京XX有限公司,2026-01-01,",
  "BOGUS,SUP000002,SRM,SRM-2024-002,MDM_MASTER,上海YY贸易,2026-01-01,",
].join("\n");

/** 1001 行合法 → 触发前端 1000 行硬上限（后端同款硬限）。 */
const OVER_LIMIT_CSV = [
  CSV_HEADER,
  ...Array.from(
    { length: 1001 },
    (_, i) =>
      `SUPPLIER,SUP${String(i).padStart(6, "0")},SRM,SRM-${i},MDM_MASTER,名称${i},2026-01-01,`,
  ),
].join("\n");

const OK_RESULT: EntityMappingBulkResult = {
  total: 1,
  inserted: 1,
  updated: 0,
  skipped: 0,
  failed: 0,
  results: [
    {
      row: 2,
      status: "inserted",
      entityType: "SUPPLIER",
      enterpriseCode: "SUP000001",
      id: 42,
      changedFields: ["name"],
      reason: null,
      error: null,
    },
  ],
};

function renderModal() {
  const onClose = vi.fn();
  const onSuccess = vi.fn();
  render(
    <ConfigProvider locale={zhCN}>
      <BulkImportModal open onClose={onClose} onSuccess={onSuccess} />
    </ConfigProvider>,
  );
  return { onClose, onSuccess };
}

function textarea(): HTMLTextAreaElement {
  return screen.getByTestId("bulk-import-input") as HTMLTextAreaElement;
}

function paste(content: string) {
  fireEvent.change(textarea(), { target: { value: content } });
}

// =============================================================================
// Tests
// =============================================================================

describe("BulkImportModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.bulkImportMappings.mockResolvedValue(OK_RESULT);
    csvDownload.readFileAsText.mockResolvedValue(VALID_CSV);
  });

  // ---- 输入 / 解析（纯前端，不打后端） ----

  it("「加载示例」灌入 TSV 样例并解析出 3 行有效数据（分隔符识别为 Tab）", async () => {
    renderModal();

    fireEvent.click(screen.getByRole("button", { name: "加载示例" }));

    await waitFor(() => {
      expect(textarea().value).toContain("entity_type\tenterprise_code");
    });
    expect(screen.getByText("解析行数：3")).toBeInTheDocument();
    expect(screen.getByText("可导入：3")).toBeInTheDocument();
    expect(screen.getByText("分隔符：Tab")).toBeInTheDocument();
    // 纯前端解析：一次后端请求都不该有
    expect(api.bulkImportMappings).not.toHaveBeenCalled();
  });

  it("「清空」把文本框内容清空", async () => {
    renderModal();
    paste(VALID_CSV);
    await waitFor(() => expect(textarea().value).not.toBe(""));

    // antd 对 2 字中文按钮名自动插空格（"清空" → "清 空"）
    fireEvent.click(screen.getByRole("button", { name: /清\s*空/ }));

    await waitFor(() => expect(textarea().value).toBe(""));
    expect(screen.queryByText("解析行数：1")).toBeNull();
  });

  it("「下载模板」把模板 SSOT 交给 csvDownload.downloadCsv", async () => {
    renderModal();

    fireEvent.click(screen.getByRole("button", { name: "下载模板" }));

    await waitFor(() => expect(csvDownload.downloadCsv).toHaveBeenCalledTimes(1));
    const [content, filename] = csvDownload.downloadCsv.mock.calls[0];
    expect(filename).toBe("entity-mapping-template.csv");
    expect(content).toContain(CSV_HEADER);
    // 模板自带 3 行示例行，字段集与文档模板一致
    expect(content.split("\n")).toHaveLength(4);
  });

  // ---- 预览（逐行校验结果） ----

  it("预览表把合法行标 OK、越界行标 FAIL 并给出失败原因", async () => {
    renderModal();
    paste(MIXED_CSV);

    await waitFor(() => expect(screen.getByText("可导入：1")).toBeInTheDocument());
    expect(screen.getByText("失败：1")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /预览/ }));

    expect(await screen.findByText("OK")).toBeInTheDocument();
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    expect(
      screen.getByText("entity_type 不在合法范围：BOGUS"),
    ).toBeInTheDocument();
    // 合法行渲染出列值
    expect(screen.getByText("SUP000001")).toBeInTheDocument();
    expect(screen.getByText("SRM-2024-001")).toBeInTheDocument();
  });

  it("日期格式非 YYYY-MM-DD 的行被前端拦下并说明期望格式", async () => {
    renderModal();
    paste(
      [
        CSV_HEADER,
        "SUPPLIER,SUP000001,SRM,SRM-2024-001,MDM_MASTER,北京XX有限公司,2026/01/01,",
      ].join("\n"),
    );

    await waitFor(() => expect(screen.getByText("可导入：0")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: /预览/ }));

    expect(
      await screen.findByText(
        "effective_date 格式不对（期望 YYYY-MM-DD）：2026/01/01",
      ),
    ).toBeInTheDocument();
  });

  // ---- 导入（唯一的后端写入口） ----

  it("导入时把有效行按 EntityMappingCreate 契约提交给 bulkImportMappings", async () => {
    api.bulkImportMappings.mockResolvedValue(OK_RESULT);
    renderModal();
    paste(VALID_CSV);
    await waitFor(() => expect(screen.getByText("可导入：1")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /导入 \(1 行\)/ }));

    await waitFor(() => expect(api.bulkImportMappings).toHaveBeenCalledTimes(1));
    expect(api.bulkImportMappings).toHaveBeenCalledWith([
      {
        entityType: "SUPPLIER",
        enterpriseKey: 0,
        enterpriseCode: "SUP000001",
        sourceSystem: "SRM",
        sourceKey: "SRM-2024-001",
        sourceCode: "SRM-2024-001",
        matchRule: "MDM_MASTER",
        effectiveDate: "2026-01-01",
        expiryDate: null,
        name: "北京XX有限公司",
      },
    ]);
  });

  it("非法行不进 payload（只提交合法行）", async () => {
    renderModal();
    paste(MIXED_CSV);
    await waitFor(() => expect(screen.getByText("可导入：1")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /导入 \(1 行\)/ }));

    await waitFor(() => expect(api.bulkImportMappings).toHaveBeenCalledTimes(1));
    const payload = api.bulkImportMappings.mock.calls[0][0];
    expect(payload).toHaveLength(1);
    expect(payload[0].sourceCode).toBe("SRM-2024-001");
  });

  it("没有合法行时导入按钮禁用，不会发起批量请求", async () => {
    renderModal();
    paste(
      [
        CSV_HEADER,
        "BOGUS,SUP000002,SRM,SRM-2024-002,MDM_MASTER,上海YY贸易,2026-01-01,",
      ].join("\n"),
    );
    await waitFor(() => expect(screen.getByText("可导入：0")).toBeInTheDocument());

    const importBtn = screen.getByRole("button", { name: /导入 \(0 行\)/ });
    expect(importBtn).toBeDisabled();
    fireEvent.click(importBtn);
    expect(api.bulkImportMappings).not.toHaveBeenCalled();
  });

  it("超过 1000 行时提示拆分且不发起批量请求", async () => {
    renderModal();
    paste(OVER_LIMIT_CSV);
    await waitFor(() =>
      expect(screen.getByText("可导入：1001")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /导入 \(1001 行\)/ }));

    await waitFor(() =>
      expect(messageMock.error).toHaveBeenCalledWith(
        "单批最多 1000 行，当前 1001 行，请先在 CSV 中拆分。",
      ),
    );
    expect(api.bulkImportMappings).not.toHaveBeenCalled();
  });

  it("导入失败时提示错误信息且不回调 onSuccess", async () => {
    api.bulkImportMappings.mockRejectedValue(new Error("后端炸了"));
    const { onSuccess } = renderModal();
    paste(VALID_CSV);
    await waitFor(() => expect(screen.getByText("可导入：1")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /导入 \(1 行\)/ }));

    await waitFor(() =>
      expect(messageMock.error).toHaveBeenCalledWith("导入失败：后端炸了"),
    );
    expect(onSuccess).not.toHaveBeenCalled();
  });

  // ---- 结果回显 ----

  it("导入成功后结果表渲染每行状态与变更字段，且 failed===0 时回调 onSuccess", async () => {
    api.bulkImportMappings.mockResolvedValue(OK_RESULT);
    const { onSuccess } = renderModal();
    paste(VALID_CSV);
    await waitFor(() => expect(screen.getByText("可导入：1")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /导入 \(1 行\)/ }));

    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(messageMock.success).toHaveBeenCalledWith(
      "导入完成：插入 1 / 更新 0 / 跳过 0 / 失败 0",
    );

    fireEvent.click(screen.getByRole("tab", { name: /结果/ }));
    expect(await screen.findByText("inserted")).toBeInTheDocument();
    expect(screen.getByText("总行数：1")).toBeInTheDocument();
    expect(screen.getByText("插入：1")).toBeInTheDocument();
    // changedFields 非空 → 逗号连接；status=inserted → 无错误文案
    expect(screen.getByText("name")).toBeInTheDocument();
  });

  it("存在失败行时不回调 onSuccess（反向用例）", async () => {
    api.bulkImportMappings.mockResolvedValue({
      total: 2,
      inserted: 1,
      updated: 0,
      skipped: 0,
      failed: 1,
      results: [
        {
          row: 2,
          status: "inserted",
          entityType: "SUPPLIER",
          enterpriseCode: "SUP000001",
          id: 42,
          changedFields: ["name", "status"],
          reason: null,
          error: null,
        },
        {
          row: 3,
          status: "failed",
          entityType: "SUPPLIER",
          enterpriseCode: "SUP000002",
          id: null,
          changedFields: null,
          reason: null,
          error: "enterprise_code 冲突",
        },
      ],
    });
    const { onSuccess } = renderModal();
    paste(VALID_CSV);
    await waitFor(() => expect(screen.getByText("可导入：1")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /导入 \(1 行\)/ }));

    await waitFor(() =>
      expect(messageMock.success).toHaveBeenCalledWith(
        "导入完成：插入 1 / 更新 0 / 跳过 0 / 失败 1",
      ),
    );
    expect(onSuccess).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: /结果/ }));
    expect(await screen.findByText("failed")).toBeInTheDocument();
    expect(screen.getByText("失败：1")).toBeInTheDocument();
    expect(screen.getByText("enterprise_code 冲突")).toBeInTheDocument();
    // changedFields 非空 → 逗号连接
    expect(screen.getByText("name, status")).toBeInTheDocument();
    // changedFields 为 null → 展示占位符
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  // ---- 上传文件 ----

  it("上传 CSV 文件：读取到的内容进文本框，并提示文件大小", async () => {
    renderModal();
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    expect(input).not.toBeNull();
    const file = new File(["abc"], "mapping.csv", { type: "text/csv" });
    Object.defineProperty(input, "files", { configurable: true, value: [file] });

    fireEvent.change(input);

    await waitFor(() =>
      expect(csvDownload.readFileAsText).toHaveBeenCalledWith(file),
    );
    await waitFor(() => expect(textarea().value).toBe(VALID_CSV));
    expect(messageMock.success).toHaveBeenCalledWith(
      "已读取文件：mapping.csv（0.0 KB）",
    );
  });

  it("上传文件读取失败时提示错误且文本框保持为空", async () => {
    csvDownload.readFileAsText.mockRejectedValue(
      new Error("不支持的文件后缀：mapping.bin（仅 .csv/.tsv/.txt）"),
    );
    renderModal();
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const file = new File(["abc"], "mapping.bin", { type: "application/octet-stream" });
    Object.defineProperty(input, "files", { configurable: true, value: [file] });

    fireEvent.change(input);

    await waitFor(() =>
      expect(messageMock.error).toHaveBeenCalledWith(
        "不支持的文件后缀：mapping.bin（仅 .csv/.tsv/.txt）",
      ),
    );
    expect(textarea().value).toBe("");
  });

  // ---- 关闭 ----

  it("「关闭」回调 onClose 并清空已填内容", async () => {
    const { onClose } = renderModal();
    paste(VALID_CSV);
    await waitFor(() => expect(textarea().value).not.toBe(""));

    // antd 对 2 字中文按钮名自动插空格（"关闭" → "关 闭"）
    fireEvent.click(screen.getByRole("button", { name: /关\s*闭/ }));

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(textarea().value).toBe("");
  });
});
