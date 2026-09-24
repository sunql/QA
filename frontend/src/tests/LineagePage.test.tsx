/** LineagePage 集成测试（Phase 2.3 RED + Step 5 对象级下拉）。
 *
 * 覆盖：
 * - mount 时拉取 edges
 * - 加载完成后渲染 LineageGraph
 * - LayerFilter 取消勾选某层 → LineageGraph 收到的 edges 不含该层
 * - 拉取失败显示错误 toast
 * - 空 edges 时显示空状态
 * - ObjectFilter 候选来自层过滤后的 edges（按层排序）
 * - 选中对象 → LineageGraph 只渲染触及该对象的边
 * - 取消某层 → 该层对象从候选移除，且对象选择被裁剪
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { message } from "antd";

// 必须把页面实际 import 的**每一个**函数都列出来：vi.mock 的工厂是整体替换，
// 漏掉的具名导出会变成 undefined —— 管理 Tab 一旦渲染就会调用它们。
vi.mock("../api/lineage", () => ({
  listEdges: vi.fn(),
  extractLineage: vi.fn(),
  createEdge: vi.fn(),
  updateEdge: vi.fn(),
  deleteEdge: vi.fn(),
}));

const echartsOptionCapture = vi.hoisted(() => ({ current: null as Record<string, unknown> | null }));

vi.mock("echarts-for-react", () => ({
  __esModule: true,
  default: (props: { option?: Record<string, unknown> }) => {
    echartsOptionCapture.current = props.option ?? null;
    return (
      <div data-testid="echarts-mock">{props.option ? "rendered" : "empty"}</div>
    );
  },
}));

// ObjectFilter 只 mock 组件外壳；纯函数（collectObjectCandidates / filterEdgesByObjects）
// 用真实实现，LineagePage 的接线逻辑才能被真实执行。
const objectFilterProps = vi.hoisted(() => ({
  current: null as {
    candidates: Array<{ layer: string; object: string; count: number }>;
    value: Set<string>;
    onChange: (next: Set<string>) => void;
  } | null,
}));

vi.mock("../components/lineage/ObjectFilter", () => ({
  __esModule: true,
  default: (props: {
    candidates: Array<{ layer: string; object: string; count: number }>;
    value: Set<string>;
    onChange: (next: Set<string>) => void;
  }) => {
    objectFilterProps.current = props;
    return <div data-testid="object-filter-mock" />;
  },
}));

import LineagePage from "../pages/LineagePage";
import * as lineageApi from "../api/lineage";
import type { LineageEdgeRead } from "../types/lineage";

function edge(overrides: Partial<LineageEdgeRead>): LineageEdgeRead {
  return {
    id: 1,
    sourceLayer: "SOURCE_SYSTEM",
    sourceSystem: "ERP",
    sourceObject: "PORDER",
    sourceField: null,
    targetLayer: "SOURCE_SYSTEM",
    targetSystem: "ERP",
    targetObject: "BPSUPPLIER",
    targetField: null,
    transformationRule: null,
    refreshFrequency: "DAILY",
    owner: null,
    description: null,
    isActive: true,
    createdTime: null,
    updatedTime: null,
    ...overrides,
  };
}

describe("LineagePage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    echartsOptionCapture.current = null;
    objectFilterProps.current = null;
  });

  it("fetches edges on mount and renders graph", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([
      edge({ id: 1, sourceObject: "PORDER", targetObject: "BPSUPPLIER" }),
      edge({ id: 2, sourceObject: "BPSUPPLIER", targetObject: "ITMMASTER" }),
    ]);
    render(<LineagePage />);
    await waitFor(() => {
      expect(screen.getByTestId("echarts-mock").textContent).toBe("rendered");
    });
    expect(lineageApi.listEdges).toHaveBeenCalledTimes(1);
  });

  it("shows error toast on fetch failure", async () => {
    const errorSpy = vi.spyOn(message, "error").mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    vi.mocked(lineageApi.listEdges).mockRejectedValue(new Error("network error"));
    render(<LineagePage />);
    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalled();
    });
    errorSpy.mockRestore();
  });

  it("non-Error rejection 走 String(error) 分支（errorMessageOf 兜底）", async () => {
    const errorSpy = vi.spyOn(message, "error").mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    vi.mocked(lineageApi.listEdges).mockRejectedValue("plain string error");
    render(<LineagePage />);
    await waitFor(() => {
      // 错误字符串透传
      expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("plain string error"));
    });
    errorSpy.mockRestore();
  });

  it("renders empty state when no edges", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([]);
    render(<LineagePage />);
    await waitFor(() => {
      // Empty state — graph not rendered; check for "暂无血缘" placeholder text
      expect(screen.queryByTestId("echarts-mock")).toBeNull();
    });
  });

  it("filters edges by selected layers", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([
      edge({ id: 1, sourceLayer: "SOURCE_SYSTEM", targetLayer: "SOURCE_SYSTEM", sourceObject: "A", targetObject: "B" }),
      edge({ id: 2, sourceLayer: "ODS", targetLayer: "DWD", sourceObject: "ODS_X", targetObject: "DWD_X" }),
    ]);
    render(<LineagePage />);
    await waitFor(() => {
      expect(screen.getByTestId("echarts-mock").textContent).toBe("rendered");
    });
    // 取消勾选 SOURCE_SYSTEM
    const checkbox = screen.getByLabelText("SOURCE_SYSTEM") as HTMLInputElement;
    fireEvent.click(checkbox);
    await waitFor(() => {
      // 过滤后只剩 ODS → DWD 一条边
      const opt = echartsOptionCapture.current as {
        series?: Array<{ nodes?: Array<{ name: string }>; links?: unknown[] }>;
      } | null;
      expect(opt).not.toBeNull();
      const nodes = opt?.series?.[0]?.nodes ?? [];
      const names = nodes.map((n) => n.name).sort();
      expect(names).toEqual(["DWD_X", "ODS_X"]);
      expect(opt?.series?.[0]?.links?.length).toBe(1);
    });
  });

  it("passes object candidates derived from layer-filtered edges", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([
      edge({
        id: 1,
        sourceLayer: "SOURCE_SYSTEM",
        targetLayer: "SOURCE_SYSTEM",
        sourceObject: "PORDER",
        targetObject: "BPSUPPLIER",
      }),
      edge({
        id: 2,
        sourceLayer: "ODS",
        targetLayer: "DWD",
        sourceObject: "ODS_PORDER",
        targetObject: "DWD_X",
      }),
    ]);
    render(<LineagePage />);
    await waitFor(() => {
      expect(objectFilterProps.current).not.toBeNull();
    });
    // 候选按层名排序：DWD < ODS < SOURCE_SYSTEM
    const layers = objectFilterProps.current!.candidates.map((c) => c.layer);
    expect(layers).toEqual(["DWD", "ODS", "SOURCE_SYSTEM", "SOURCE_SYSTEM"]);
    const objects = objectFilterProps.current!.candidates.map((c) => c.object).sort();
    expect(objects).toEqual(["BPSUPPLIER", "DWD_X", "ODS_PORDER", "PORDER"]);
  });

  it("narrows graph to edges touching the selected object", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([
      edge({ id: 1, sourceObject: "PORDER", targetObject: "BPSUPPLIER" }),
      edge({ id: 2, sourceObject: "BPSUPPLIER", targetObject: "ITMMASTER" }),
    ]);
    render(<LineagePage />);
    await waitFor(() => {
      expect(screen.getByTestId("echarts-mock").textContent).toBe("rendered");
    });
    // 模拟用户在 ObjectFilter 选中 PORDER（纯函数被真实执行）
    objectFilterProps.current!.onChange(new Set(["SOURCE_SYSTEM/PORDER"]));
    await waitFor(() => {
      const opt = echartsOptionCapture.current as {
        series?: Array<{ nodes?: Array<{ name: string }> }>;
      } | null;
      const names = (opt?.series?.[0]?.nodes ?? []).map((n) => n.name).sort();
      // 只剩 PORDER → BPSUPPLIER 一条边；ITMMASTER 不再出现
      expect(names).toEqual(["BPSUPPLIER", "PORDER"]);
    });
  });

  it("extract: created>0 → toast 新增数 + 拉取刷新", async () => {
    const successSpy = vi
      .spyOn(message, "success")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.success>);
    vi.mocked(lineageApi.listEdges).mockResolvedValue([]);
    vi.mocked(lineageApi.extractLineage).mockResolvedValue({ created: 3 });
    render(<LineagePage />);
    await waitFor(() => {
      expect(lineageApi.listEdges).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole("button", { name: /自动抽取血缘/ }));

    await waitFor(() => {
      expect(lineageApi.extractLineage).toHaveBeenCalledTimes(1);
    });
    expect(successSpy).toHaveBeenCalledWith(expect.stringContaining("3"));
    // 抽取成功后自动 refresh → 第二次 listEdges
    await waitFor(() => {
      expect(lineageApi.listEdges).toHaveBeenCalledTimes(2);
    });
    successSpy.mockRestore();
  });

  it("extract: created=0 → info 提示无新增（幂等）且仍刷新", async () => {
    const infoSpy = vi
      .spyOn(message, "info")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.info>);
    vi.mocked(lineageApi.listEdges).mockResolvedValue([]);
    vi.mocked(lineageApi.extractLineage).mockResolvedValue({ created: 0 });
    render(<LineagePage />);
    await waitFor(() => {
      expect(lineageApi.listEdges).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole("button", { name: /自动抽取血缘/ }));

    await waitFor(() => {
      expect(infoSpy).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(lineageApi.listEdges).toHaveBeenCalledTimes(2);
    });
    infoSpy.mockRestore();
  });

  it("extract failure → error toast，不崩溃", async () => {
    const errorSpy = vi
      .spyOn(message, "error")
      .mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
    vi.mocked(lineageApi.listEdges).mockResolvedValue([]);
    vi.mocked(lineageApi.extractLineage).mockRejectedValue(new Error("extract boom"));
    render(<LineagePage />);
    await waitFor(() => {
      expect(lineageApi.listEdges).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByRole("button", { name: /自动抽取血缘/ }));

    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("extract boom"));
    });
    errorSpy.mockRestore();
  });

  it("prunes selected objects when their layer is deselected", async () => {
    vi.mocked(lineageApi.listEdges).mockResolvedValue([
      edge({
        id: 1,
        sourceLayer: "SOURCE_SYSTEM",
        targetLayer: "SOURCE_SYSTEM",
        sourceObject: "PORDER",
        targetObject: "BPSUPPLIER",
      }),
      edge({
        id: 2,
        sourceLayer: "ODS",
        targetLayer: "DWD",
        sourceObject: "ODS_PORDER",
        targetObject: "DWD_X",
      }),
    ]);
    render(<LineagePage />);
    await waitFor(() => {
      expect(objectFilterProps.current).not.toBeNull();
    });
    // 先选中 SOURCE_SYSTEM 层的 PORDER
    objectFilterProps.current!.onChange(new Set(["SOURCE_SYSTEM/PORDER"]));
    // 再取消 SOURCE_SYSTEM 层
    const checkbox = screen.getByLabelText("SOURCE_SYSTEM") as HTMLInputElement;
    fireEvent.click(checkbox);
    await waitFor(() => {
      const layers = objectFilterProps.current!.candidates.map((c) => c.layer);
      expect(layers).toEqual(["DWD", "ODS"]);
      // effectiveSelected 裁剪：PORDER 已不在候选 → value 为空
      expect(objectFilterProps.current!.value.size).toBe(0);
    });
  });
});

/**
 * 管理 Tab（血缘边 CRUD）。
 *
 * 页面被 Tab 化之后，这两件事最容易在改动中悄悄丢掉：管理视图要看到**全部**边
 * （含已停用，否则停用的边再也改不回来），以及删除必须先二次确认。
 */
describe("LineagePage 管理 Tab", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    echartsOptionCapture.current = null;
    objectFilterProps.current = null;
  });

  /** 渲染 → 等首屏真的落定 → 切到「管理」→ 等管理面板渲染出来。
   *
   * 两处 waitFor 都不是「等一等」，而是把异步 setState 收进 act 里：
   * - 首屏：listEdges 落定后会 setEdges，只等「调用发生」会抢在落定之前，
   *   React 就会报 GraphTab 的更新未包裹在 act 中；
   * - 切 Tab：ManageTab 挂载后自己再拉一次，同理。
   * 本组用例都至少给了一条边，所以「图渲染出来」就是首屏落定的可观测信号。
   *
   * 超时全部显式放宽：单跑本文件时这些用例 ~2s，全量跑（112 个文件抢 CPU）会涨到
   * 6s+，撞穿 vitest 默认的 5s testTimeout，表现为「单跑全绿、全量随机红一条」。
   * 断言本身没变，只是不再把机器负载当成被测行为。 */
  const FIND_TIMEOUT = 10_000;
  const TEST_TIMEOUT = 20_000;

  async function switchToManageTab(): Promise<void> {
    render(<LineagePage />);
    await waitFor(
      () => expect(screen.getByTestId("echarts-mock")).toBeInTheDocument(),
      { timeout: FIND_TIMEOUT },
    );
    fireEvent.click(screen.getByRole("tab", { name: /管\s*理/ }));
    await waitFor(
      () => expect(screen.getByRole("button", { name: /新建血缘边/ })).toBeInTheDocument(),
      { timeout: FIND_TIMEOUT },
    );
  }

  it(
    "管理视图拉的是全部边（不带 activeOnly），不是可视化那份活跃边",
    async () => {
      vi.mocked(lineageApi.listEdges).mockResolvedValue([edge({ id: 7 })]);
      await switchToManageTab();

      // 可视化先拉一次，带 activeOnly
      expect(lineageApi.listEdges).toHaveBeenCalledWith({ activeOnly: true });
      // 管理 Tab 再拉一次，**不带** activeOnly —— 已停用的边也要能看见、能改回来
      await waitFor(() => expect(lineageApi.listEdges).toHaveBeenCalledWith({}), {
        timeout: FIND_TIMEOUT,
      });
    },
    TEST_TIMEOUT,
  );

  it(
    "删除一条边要先二次确认，只点「删 除」不发请求",
    async () => {
      vi.mocked(lineageApi.listEdges).mockResolvedValue([edge({ id: 9 })]);
      await switchToManageTab();

      // antd 会在两个汉字之间插空格，可访问名是「删 除」；^$ 锚定避免误命中页面顶部按钮
      const deleteBtn = await screen.findByRole(
        "button",
        { name: /^删\s*除$/ },
        { timeout: FIND_TIMEOUT },
      );
      fireEvent.click(deleteBtn);

      expect(lineageApi.deleteEdge).not.toHaveBeenCalled();
      expect(
        await screen.findByText("确认删除该血缘边？", undefined, {
          timeout: FIND_TIMEOUT,
        }),
      ).toBeInTheDocument();
    },
    TEST_TIMEOUT,
  );

  it(
    "管理 Tab 加载失败时给出提示且不崩",
    async () => {
      const errorSpy = vi
        .spyOn(message, "error")
        .mockReturnValue(1 as unknown as ReturnType<typeof message.error>);
      vi.mocked(lineageApi.listEdges)
        .mockResolvedValueOnce([edge({ id: 7 })]) // 可视化的首屏
        .mockRejectedValue(new Error("edges boom")); // 管理 Tab 的拉取

      await switchToManageTab();

      await waitFor(
        () => {
          expect(errorSpy).toHaveBeenCalledWith(
            expect.stringContaining("加载血缘边列表失败"),
          );
        },
        { timeout: FIND_TIMEOUT },
      );
      errorSpy.mockRestore();
    },
    TEST_TIMEOUT,
  );

  it(
    "管理 Tab 分页：showSizeChanger 已挂载，pageSizeOptions 候选全在",
    async () => {
      vi.mocked(lineageApi.listEdges).mockResolvedValue([edge({ id: 11 })]);
      await switchToManageTab();

      // 验证 .ant-pagination-options-size-changer 已挂载（即 showSizeChanger: true）
      await waitFor(
        () => {
          expect(
            document.querySelector(
              ".ant-pagination-options-size-changer",
            ),
          ).toBeTruthy();
        },
        { timeout: FIND_TIMEOUT },
      );
      // 同时验证 antd 渲染出来的「共 N 条」summary（showTotal）
      expect(document.querySelector(".ant-pagination-total-text")).toBeTruthy();
    },
    TEST_TIMEOUT,
  );

  it(
    "管理 Tab 分页：localStorage 记忆 pageSize 切到 50 后刷新仍是 50",
    async () => {
      // 预置 localStorage 记忆
      window.localStorage.setItem("qa.lineage.manage.pageSize", "50");

      vi.mocked(lineageApi.listEdges).mockResolvedValue([edge({ id: 22 })]);
      await switchToManageTab();

      // 验证 localStorage 值已被读到（pageSize 用 50 不是默认 20）
      // 间接证据：原值 "50" 在 mount 时被读，且 useEffect 立刻写回（值不变），
      // 因此 localStorage 仍是 "50"；默认值 20 路径下会是 "20"。
      await waitFor(
        () => {
          expect(
            window.localStorage.getItem("qa.lineage.manage.pageSize"),
          ).toBe("50");
        },
        { timeout: FIND_TIMEOUT },
      );
      // 同时校验：showSizeChanger 渲染中（证明受控 pagination 配置生效）
      expect(
        document.querySelector(".ant-pagination-options-size-changer"),
      ).toBeTruthy();

      // 清理
      window.localStorage.removeItem("qa.lineage.manage.pageSize");
    },
    TEST_TIMEOUT,
  );

  it(
    "管理 Tab 分页：非法 localStorage 值（不在候选里）回退到默认 20",
    async () => {
      window.localStorage.setItem("qa.lineage.manage.pageSize", "999");

      vi.mocked(lineageApi.listEdges).mockResolvedValue([edge({ id: 33 })]);
      await switchToManageTab();

      // useEffect 把 20 写回去 → 非法值被覆写为默认值
      await waitFor(
        () => {
          expect(
            window.localStorage.getItem("qa.lineage.manage.pageSize"),
          ).toBe("20");
        },
        { timeout: FIND_TIMEOUT },
      );

      window.localStorage.removeItem("qa.lineage.manage.pageSize");
    },
    TEST_TIMEOUT,
  );
});
