import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import SemanticRelationTab from "../components/ontology/SemanticRelationTab";
import type { OntologyClass, OntologySemanticRelation } from "../types/ontology";

// =============================================================================
// Mocks
// =============================================================================

const mockClass: OntologyClass = {
  id: 1,
  className: "PRECEIPT",
  classAlias: "收货单",
  description: null,
  sourceTable: "PRECEIPT",
  parentClassId: null,
  objectType: null,
  objectOwner: null,
  createdBy: null,
  createdTime: "2026-08-11T00:00:00Z",
  updatedTime: "2026-08-11T00:00:00Z",
  version: 1,
  validFrom: "2026-08-11T00:00:00Z",
  validTo: null,
};

const mockClass2: OntologyClass = {
  ...mockClass,
  id: 2,
  className: "BPARTNER",
  classAlias: "供应商",
};

const mockRelation: OntologySemanticRelation = {
  id: 10,
  sourceClassId: 1,
  targetClassId: 2,
  relationType: "SUPPLIES",
  description: "供应商供货",
  createdBy: null,
  createdTime: "2026-08-11T00:00:00Z",
  updatedTime: "2026-08-11T00:00:00Z",
};

const api = vi.hoisted(() => ({
  listSemanticRelations: vi.fn(),
  createSemanticRelation: vi.fn(),
  deleteSemanticRelation: vi.fn(),
  backfillRelations: vi.fn(),
}));

vi.mock("../api/ontology", () => api);

function renderTab(classes: OntologyClass[] = [mockClass, mockClass2]) {
  return render(
    <ConfigProvider locale={zhCN}>
      <SemanticRelationTab classes={classes} />
    </ConfigProvider>,
  );
}

// =============================================================================
// 渲染 + 加载
// =============================================================================

describe("SemanticRelationTab — 渲染与加载", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listSemanticRelations.mockResolvedValue([mockRelation]);
  });

  it("挂载时加载语义关系列表并展示源类与类型 label", async () => {
    renderTab();
    expect(api.listSemanticRelations).toHaveBeenCalled();
    expect(await screen.findByText(/PRECEIPT/)).toBeInTheDocument();
    // relationType 经 i18n 解析为 enums.semanticRelationType.SUPPLIES（zh-CN）
    expect(screen.getByText("供货 SUPPLIES")).toBeInTheDocument();
  });

  it("无 class 时「新增语义关系」按钮禁用", () => {
    api.listSemanticRelations.mockResolvedValue([]);
    renderTab([]);
    const addBtn = screen.getByRole("button", { name: /新增\s*语义\s*关系/ });
    expect(addBtn).toBeDisabled();
  });

  it("加载失败时表格保持空（错误已由拦截器处理，组件不崩）", async () => {
    api.listSemanticRelations.mockRejectedValue(new Error("网络错误"));
    renderTab();
    await waitFor(() => expect(api.listSemanticRelations).toHaveBeenCalled());
  });
});

// =============================================================================
// 提交（handleSubmit）— 表单必填校验 + 异常分支
// =============================================================================

describe("SemanticRelationTab — handleSubmit 校验 catch", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listSemanticRelations.mockResolvedValue([]);
    api.createSemanticRelation.mockResolvedValue({ id: 99 });
  });

  it("打开弹窗后不填任何字段直接提交 → 必填校验失败被 catch，createSemanticRelation 不被调用", async () => {
    const user = userEvent.setup();
    renderTab();

    await user.click(screen.getByRole("button", { name: /新增\s*语义\s*关系/ }));

    const okBtn = await screen.findByRole("button", { name: /确\s?定$/ });
    await user.click(okBtn);

    await waitFor(() => {
      expect(api.createSemanticRelation).not.toHaveBeenCalled();
    });
  });

  it("点击取消关闭弹窗且不调用 createSemanticRelation", async () => {
    const user = userEvent.setup();
    renderTab();

    await user.click(screen.getByRole("button", { name: /新增\s*语义\s*关系/ }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();

    const cancelBtn = await screen.findByRole("button", { name: /取\s?消/ });
    await user.click(cancelBtn);

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.createSemanticRelation).not.toHaveBeenCalled();
  });
});

// =============================================================================
// 删除
// =============================================================================

describe("SemanticRelationTab — handleDelete", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listSemanticRelations.mockResolvedValue([mockRelation]);
    api.deleteSemanticRelation.mockResolvedValue(undefined);
  });

  it("点击删除按钮 + Popconfirm 确认 → 调用 deleteSemanticRelation 并 reload", async () => {
    const user = userEvent.setup();
    renderTab();

    const deleteBtn = await screen.findByRole("button", { name: /删\s?除/ });
    await user.click(deleteBtn);

    const confirmBtn = await screen.findByRole("button", { name: /确\s?定$/ });
    await user.click(confirmBtn);

    await waitFor(() => expect(api.deleteSemanticRelation).toHaveBeenCalledWith(10));
    await waitFor(() => expect(api.listSemanticRelations).toHaveBeenCalledTimes(2));
  });

  it("删除失败时 catch 分支被吞掉（不再 reload、不崩）", async () => {
    api.deleteSemanticRelation.mockRejectedValue(new Error("权限不足"));
    const user = userEvent.setup();
    renderTab();

    const deleteBtn = await screen.findByRole("button", { name: /删\s?除/ });
    await user.click(deleteBtn);
    const confirmBtn = await screen.findByRole("button", { name: /确\s?定$/ });
    await user.click(confirmBtn);

    await waitFor(() => expect(api.deleteSemanticRelation).toHaveBeenCalledWith(10));
    await waitFor(() => expect(api.listSemanticRelations).toHaveBeenCalledTimes(1));
  });
});

// =============================================================================
// 一键补关系（backfill）
// =============================================================================

describe("SemanticRelationTab — 一键补关系", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listSemanticRelations.mockResolvedValue([mockRelation]);
    api.backfillRelations.mockResolvedValue({ syncedJoins: 3, backfilledReferences: 2 });
  });

  it("点击 + Popconfirm 确认 → 调用 backfillRelations 并 reload", async () => {
    const user = userEvent.setup();
    renderTab();

    const backfillBtn = await screen.findByRole("button", { name: /一\s*键\s*补\s*关\s*系/ });
    await user.click(backfillBtn);

    const confirmBtn = await screen.findByRole("button", { name: /确\s?定$/ });
    await user.click(confirmBtn);

    await waitFor(() => expect(api.backfillRelations).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.listSemanticRelations).toHaveBeenCalledTimes(2));
  });

  it("backfill 失败时 catch 分支被吞掉（不崩、不 reload）", async () => {
    api.backfillRelations.mockRejectedValue(new Error("Neo4j 不可达"));
    const user = userEvent.setup();
    renderTab();

    const backfillBtn = await screen.findByRole("button", { name: /一\s*键\s*补\s*关\s*系/ });
    await user.click(backfillBtn);
    const confirmBtn = await screen.findByRole("button", { name: /确\s?定$/ });
    await user.click(confirmBtn);

    await waitFor(() => expect(api.backfillRelations).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.listSemanticRelations).toHaveBeenCalledTimes(1));
  });
});

// =============================================================================
// 列渲染边界：description=null → emDash
// =============================================================================

describe("SemanticRelationTab — 列渲染边界", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    const relNoDesc: OntologySemanticRelation = { ...mockRelation, id: 11, description: null };
    api.listSemanticRelations.mockResolvedValue([relNoDesc]);
  });

  it("description=null 时渲染 emDash（—）", async () => {
    renderTab();
    expect(await screen.findByText(/PRECEIPT/)).toBeInTheDocument();
    await waitFor(() => {
      const cells = document.querySelectorAll("td");
      const hasEmDash = Array.from(cells).some(
        (td) =>
          (td.textContent || "").trim() === "—" ||
          (td.textContent || "").trim() === "common.emDash",
      );
      expect(hasEmDash).toBe(true);
    });
  });
});
