import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import EntityMappingPage from "../pages/EntityMappingPage";
import type { EntityMappingRead } from "../types/entityMapping";

const { mockMapping } = vi.hoisted(() => ({
  mockMapping: {
    id: 1,
    entityType: "SUPPLIER",
    enterpriseKey: 100001,
    enterpriseCode: "SUP000001",
    sourceSystem: "ERP",
    sourceKey: "V000001",
    sourceCode: "V000001-CODE",
    matchRule: "MDM_MASTER",
    effectiveDate: "2026-01-01",
    expiryDate: null,
    createdTime: "2026-08-30T00:00:00Z",
    updatedTime: "2026-08-30T00:00:00Z",
  } as EntityMappingRead,
}));

const api = vi.hoisted(() => ({
  listMappings: vi.fn(),
  createMapping: vi.fn(),
  updateMapping: vi.fn(),
  deleteMapping: vi.fn(),
}));

vi.mock("../api/entityMapping", () => api);

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <MemoryRouter initialEntries={["/entity-mapping"]}>
        <Routes>
          <Route path="/entity-mapping" element={<EntityMappingPage />} />
        </Routes>
      </MemoryRouter>
    </ConfigProvider>
  );
}

describe("EntityMappingPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.listMappings.mockResolvedValue([mockMapping]);
  });

  it("渲染标题与表格，并加载映射列表", async () => {
    renderPage();

    expect(screen.getByText("新建映射")).toBeInTheDocument();

    // 等待异步数据加载后行渲染（企业编码 / 源系统 / 匹配规则列）
    await waitFor(() => {
      expect(screen.getByText("SUP000001")).toBeInTheDocument();
      expect(screen.getByText("V000001-CODE")).toBeInTheDocument();
      expect(screen.getByText("MDM_MASTER")).toBeInTheDocument();
    });
  });

  it("渲染操作列按钮", async () => {
    renderPage();
    // antd 对无 icon 的两字中文按钮自动插入空格（"编 辑"/"删 除"），用正则容错
    await waitFor(() => {
      expect(screen.getByText(/编\s?辑/)).toBeInTheDocument();
      expect(screen.getByText(/删\s?除/)).toBeInTheDocument();
    });
  });

  it("点击新建并提交调用 createMapping（camelCase payload）", async () => {
    const user = userEvent.setup();
    api.createMapping.mockResolvedValue({ ...mockMapping, id: 2 });
    renderPage();
    await waitFor(() => expect(screen.getByText("SUP000001")).toBeInTheDocument());

    await user.click(screen.getByText("新建映射"));

    // 实体类型下拉（combobox 顺序：顶部筛选=0，表单 entityType=1，sourceSystem=2，matchRule=3）
    const comboboxes = screen.getAllByRole("combobox");
    await user.click(comboboxes[1]);
    // 可点击选项是 .ant-select-item-option（带 title 属性）；role=option 的是不可点击的 a11y 复制层
    await user.click(await screen.findByTitle("MATERIAL"));

    // 企业代理键（InputNumber）
    await user.type(screen.getByRole("spinbutton"), "200001");
    // 企业编码 / 源系统 Key / 源系统编码
    await user.type(screen.getByLabelText("企业编码"), "MAT000001");
    await user.type(screen.getByLabelText("源系统 Key"), "M000001");
    await user.type(screen.getByLabelText("源系统编码"), "M000001");

    // 源系统下拉
    await user.click(screen.getAllByRole("combobox")[2]);
    await user.click(await screen.findByTitle("SRM"));

    // 模态框确定按钮（okText="保存" → antd 加空格："保 存"）
    await user.click(screen.getByRole("button", { name: /保\s?存$/ }));

    await waitFor(() => {
      expect(api.createMapping).toHaveBeenCalledTimes(1);
      const payload = api.createMapping.mock.calls[0][0];
      expect(payload.entityType).toBe("MATERIAL");
      expect(payload.enterpriseKey).toBe(200001);
      expect(payload.enterpriseCode).toBe("MAT000001");
      expect(payload.sourceSystem).toBe("SRM");
      expect(payload.sourceKey).toBe("M000001");
      expect(payload.sourceCode).toBe("M000001");
    });
  });

  it("点击编辑打开表单并提交调用 updateMapping", async () => {
    const user = userEvent.setup();
    api.updateMapping.mockResolvedValue({ ...mockMapping });
    renderPage();
    await waitFor(() => expect(screen.getByText("SUP000001")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /编\s?辑/ }));
    // 表单预填：企业编码 / 源系统编码
    expect(screen.getByLabelText("企业编码")).toHaveValue("SUP000001");
    const sourceCodeInput = screen.getByLabelText("源系统编码");
    expect(sourceCodeInput).toHaveValue("V000001-CODE");

    // 修改源系统编码后提交
    await user.clear(sourceCodeInput);
    await user.type(sourceCodeInput, "V000001-X");
    await user.click(screen.getByRole("button", { name: /保\s?存$/ }));

    await waitFor(() => {
      expect(api.updateMapping).toHaveBeenCalledTimes(1);
      expect(api.updateMapping.mock.calls[0][0]).toBe(1);
      const payload = api.updateMapping.mock.calls[0][1];
      expect(payload.sourceCode).toBe("V000001-X");
    });
  });

  it("点击删除并在确认后调用 deleteMapping", async () => {
    const user = userEvent.setup();
    api.deleteMapping.mockResolvedValue(undefined);
    renderPage();
    await waitFor(() => expect(screen.getByText("SUP000001")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /删\s?除/ }));
    const confirmBtns = screen.getAllByRole("button", { name: /确\s?定$/ });
    await user.click(confirmBtns[confirmBtns.length - 1]);

    await waitFor(() => {
      expect(api.deleteMapping).toHaveBeenCalledWith(1);
    });
  });

  it("切换实体类型筛选后按 entityType 重新拉取", async () => {
    const user = userEvent.setup();
    api.listMappings.mockResolvedValue([mockMapping]);
    renderPage();
    await waitFor(() => expect(screen.getByText("SUP000001")).toBeInTheDocument());

    // 顶部筛选下拉是第一个 combobox（无 a11y 复制层，findByText 即可唯一命中）
    await user.click(screen.getAllByRole("combobox")[0]);
    await user.click(await screen.findByText("NCR"));

    await waitFor(() => {
      expect(api.listMappings).toHaveBeenCalledWith({ entityType: "NCR" });
    });
  });
});
