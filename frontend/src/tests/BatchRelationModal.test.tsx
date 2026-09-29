import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider, message } from "antd";
import zhCN from "antd/locale/zh_CN";
import BatchRelationModal from "../components/ontology/BatchRelationModal";
import type { BatchRelationResult } from "../types/ontology";

// =============================================================================
// Mocks
// =============================================================================

const api = vi.hoisted(() => ({
  runOntologyBatch: vi.fn(),
  previewOntologyBatch: vi.fn(),
  downloadBatchTemplate: vi.fn(),
  parseBatchCsv: vi.fn(),
}));

vi.mock("../api/ontology", () => api);

const emptyCounts = { created: 0, skipped: 0, overwritten: 0, errors: [] };
const emptyResult: BatchRelationResult = {
  syncGraph: null,
  inferredJoins: [],
  joins: { ...emptyCounts },
  relations: { ...emptyCounts },
};

function renderModal() {
  return render(
    <ConfigProvider locale={zhCN}>
      <BatchRelationModal open onClose={() => {}} />
    </ConfigProvider>,
  );
}

async function enableAction(label: RegExp) {
  await userEvent.click(screen.getByRole("checkbox", { name: label }));
}

// =============================================================================
// Tests
// =============================================================================

describe("BatchRelationModal — 渲染", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(message, "success").mockImplementation(() => undefined as never);
    vi.spyOn(message, "info").mockImplementation(() => undefined as never);
    vi.spyOn(message, "warning").mockImplementation(() => undefined as never);
  });

  it("未勾选任何动作时预览/执行禁用", () => {
    renderModal();
    expect(screen.getByText("批量关系引擎")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "本体入图" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /按共享列推断/ })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "应用关系清单" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /预\s*览/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /执\s*行/ })).toBeDisabled();
    // 未选任何动作直接点预览 → 客户端错误提示，不调 API
    expect(api.previewOntologyBatch).not.toHaveBeenCalled();
  });

  it("勾选动作后预览调用 previewOntologyBatch 并展示推断候选与计数", async () => {
    const result: BatchRelationResult = {
      ...emptyResult,
      inferredJoins: [
        {
          sourceClassId: 1,
          sourceClassName: "PORDER",
          sourceColumns: ["BPRNUM_0"],
          targetClassId: 2,
          targetClassName: "BPARTNER",
          targetColumns: ["BPRNUM_0"],
          relationType: "foreign_key",
          inferredBy: "name_convention",
        },
      ],
      joins: { created: 1, skipped: 0, overwritten: 0, errors: [] },
    };
    api.previewOntologyBatch.mockResolvedValue(result);

    renderModal();
    await enableAction(/按共享列推断/);
    await userEvent.click(screen.getByRole("button", { name: /预\s*览/ }));

    await waitFor(() =>
      expect(api.previewOntologyBatch).toHaveBeenCalledWith({
        syncGraph: false,
        inferJoins: true,
        applyManifest: false,
        onConflict: "skip",
      })
    );
    // 预览结果表格：候选行（源/目标类 + 推断方式标签）
    expect(await screen.findByText("PORDER")).toBeInTheDocument();
    expect(screen.getByText("BPARTNER")).toBeInTheDocument();
    expect(screen.getByText("X3 命名约定")).toBeInTheDocument();
    // JOIN 处理计数：预览可新建 1 条
    expect(screen.getByText("物理关联 JOIN")).toBeInTheDocument();
    expect(screen.getByText(/新建 1/)).toBeInTheDocument();
  });

  it("选择覆盖策略后执行携带 overwrite 与 manifest，成功后展示结果计数", async () => {
    const result: BatchRelationResult = {
      ...emptyResult,
      joins: { created: 0, skipped: 0, overwritten: 1, errors: [] },
      relations: { created: 1, skipped: 0, overwritten: 0, errors: [] },
    };
    api.runOntologyBatch.mockResolvedValue(result);

    renderModal();
    // 勾选「应用清单」→ JSON 文本域出现
    await enableAction(/应用关系清单/);
    await userEvent.click(screen.getByRole("checkbox", { name: "本体入图" }));
    await userEvent.click(screen.getByText("覆盖（更新差异字段）"));

    const manifest = JSON.stringify({
      joins: [
        {
          sourceClassId: 1,
          sourceColumns: ["BPRNUM_0"],
          targetClassId: 2,
          targetColumns: ["BPRNUM_0"],
          joinType: "INNER",
        },
      ],
      relations: [
        { sourceClassId: 1, targetClassId: 2, relationType: "SUPPLIES" },
      ],
    });
    fireEvent.change(screen.getByRole("textbox"), { target: { value: manifest } });
    await userEvent.click(screen.getByRole("button", { name: /执\s*行/ }));

    await waitFor(() => expect(api.runOntologyBatch).toHaveBeenCalledTimes(1));
    const [payload] = api.runOntologyBatch.mock.calls[0] as [Record<string, unknown>];
    expect(payload).toEqual({
      syncGraph: true,
      inferJoins: false,
      applyManifest: true,
      onConflict: "overwrite",
      manifest: {
        joins: [
          {
            sourceClassId: 1,
            sourceColumns: ["BPRNUM_0"],
            targetClassId: 2,
            targetColumns: ["BPRNUM_0"],
            joinType: "INNER",
          },
        ],
        relations: [
          { sourceClassId: 1, targetClassId: 2, relationType: "SUPPLIES" },
        ],
      },
    });
    // 执行结果计数展示
    expect(await screen.findByText(/覆盖 1/)).toBeInTheDocument();
    expect(screen.getByText(/新建 1/)).toBeInTheDocument();
  });

  it("manifest JSON 非法时客户端拦截，不调 API", async () => {
    renderModal();
    await enableAction(/应用关系清单/);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "{not json" } });
    await userEvent.click(screen.getByRole("button", { name: /执\s*行/ }));

    expect(await screen.findByText(/JSON 解析失败/)).toBeInTheDocument();
    expect(api.runOntologyBatch).not.toHaveBeenCalled();
  });

  it("切换 CSV 来源显示上传与模板下载，点模板按钮调 downloadBatchTemplate", async () => {
    renderModal();
    await enableAction(/应用关系清单/);
    // Segmented 切到 CSV
    await userEvent.click(screen.getByText("CSV"));
    expect(screen.getByText(/点击或拖拽 CSV/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /下载语义关系模板/ }));
    expect(api.downloadBatchTemplate).toHaveBeenCalledWith("relations");
    await userEvent.click(screen.getByRole("button", { name: /下载 JOIN 模板/ }));
    expect(api.downloadBatchTemplate).toHaveBeenCalledWith("joins");
  });

  it("CSV 文件选择后调 parseBatchCsv 回填 manifest", async () => {
    const parsed = {
      manifest: {
        joins: [],
        relations: [
          { sourceClassId: 1, targetClassId: 2, relationType: "SUPPLIES" },
        ],
      },
      errors: [],
    };
    api.parseBatchCsv.mockResolvedValue(parsed);

    renderModal();
    await enableAction(/应用关系清单/);
    await userEvent.click(screen.getByText("CSV"));

    const file = new File(
      ["sourceClassName,targetClassName,relationType,description\nPORDER,BPARTNER,SUPPLIES,x\n"],
      "relations.csv",
      { type: "text/csv" }
    );
    // Modal 渲染在 body 门户，用 document 查询 Dragger 的 file input
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).not.toBeNull();
    Object.defineProperty(input, "files", {
      configurable: true,
      value: [file],
    });
    fireEvent.change(input);

    await waitFor(() =>
      expect(api.parseBatchCsv).toHaveBeenCalledWith(
        expect.any(File),
        "relations"
      )
    );
  });

  it("成功无变更（全跳过）时 toast 提示 noChangeToast", async () => {
    api.runOntologyBatch.mockResolvedValue({ ...emptyResult });
    renderModal();
    await enableAction(/按共享列推断/);
    await userEvent.click(screen.getByRole("button", { name: /执\s*行/ }));

    await waitFor(() => expect(api.runOntologyBatch).toHaveBeenCalled());
    expect(message.info).toHaveBeenCalledWith("执行完成：无任何变更（全部被跳过）");
  });

  it("CSV 清单类型选 JOIN 后上传，parseBatchCsv 携带对应 kind", async () => {
    api.parseBatchCsv.mockResolvedValue({
      manifest: { joins: [], relations: [] },
      errors: [],
    });
    renderModal();
    await enableAction(/应用关系清单/);
    await userEvent.click(screen.getByText("CSV"));
    // 默认语义关系；切到 JOIN
    await userEvent.click(screen.getByRole("radio", { name: "物理关联 JOIN" }));
    expect(api.parseBatchCsv).not.toHaveBeenCalled();

    const file = new File(["a,b\n"], "joins.csv", { type: "text/csv" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    fireEvent.change(input);
    await waitFor(() =>
      expect(api.parseBatchCsv).toHaveBeenCalledWith(expect.any(File), "joins")
    );
  });

  it("CSV 清单类型在 relations/joins 间切换会清空旧解析结果，避免串用", async () => {
    api.parseBatchCsv.mockResolvedValue({
      manifest: { joins: [], relations: [] },
      errors: [],
    });
    renderModal();
    await enableAction(/应用关系清单/);
    await userEvent.click(screen.getByText("CSV"));

    const file = new File(["a,b\n"], "rel.csv", { type: "text/csv" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    Object.defineProperty(input, "files", { configurable: true, value: [file] });
    fireEvent.change(input);
    await waitFor(() => expect(api.parseBatchCsv).toHaveBeenCalledTimes(1));
    expect(api.parseBatchCsv).toHaveBeenLastCalledWith(expect.any(File), "relations");

    // 切到 JOIN 清单类型：不应额外触发解析，仅清空旧 manifest
    await userEvent.click(screen.getByRole("radio", { name: "物理关联 JOIN" }));
    expect(api.parseBatchCsv).toHaveBeenCalledTimes(1);
  });

  it("manifest 元素缺字段/类型错时客户端拦截并指明出错位置，不调 API", async () => {
    renderModal();
    await enableAction(/应用关系清单/);
    // relations[0] 缺 relationType；joins 用空对象 —— 深校验应命中首处
    fireEvent.change(screen.getByRole("textbox"), {
      target: {
        value: JSON.stringify({
          relations: [{ sourceClassId: 1, targetClassId: 2 }],
        }),
      },
    });
    await userEvent.click(screen.getByRole("button", { name: /执\s*行/ }));
    expect(await screen.findByText(/relations\[0\].*relationType/)).toBeInTheDocument();
    expect(api.runOntologyBatch).not.toHaveBeenCalled();
  });
});
