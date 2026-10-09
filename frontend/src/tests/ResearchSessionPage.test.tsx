// ResearchSessionPage 页面级测试（feat-research-entry 修复轮）。
//
// 重点覆盖两条修复：
// 1. streaming 期间，页面由 store.events（SSE 累积事件）派生并渲染实时进度——
//    若进度视图没渲染任何东西，本测试会红（`getByText("执行查询")` 找不到）。
// 2. 首轮编排串行化：流建立（onOpen）之前不得提交首轮（后端不重放历史），
//    提交失败也不能产生未处理拒绝。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, within, act } from "@testing-library/react";
import { App, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { useResearchStore } from "../stores/researchStore";
import { useAuthStore } from "../stores/authStore";
import ResearchSessionPage from "../pages/research/ResearchSessionPage";

// REST 走 httpClient；SSE 走裸 fetch（stubGlobal）。与 researchStore.test.ts 同一 mock 约定。
const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock, apiClient: httpMock }));

function openStream(...frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(frames.join("")));
      // 不 close：流保持打开，streaming 恒真，供观察实时进度。
    },
  });
}

function stubOpenFetch(...frames: string[]): void {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: openStream(...frames) }));
}

interface DetailFixture {
  session: {
    id: string;
    title: string;
    mode: string;
    status: string;
    question: string;
    createdAt: string;
    updatedAt: string;
  };
  turns: unknown[];
  pendingCheckpoint: null;
}

function makeDetail(): { data: DetailFixture } {
  return {
    data: {
      session: {
        id: "s1",
        title: "研究：供应商 360°",
        mode: "research",
        status: "running",
        question: "供应商 360° 全景",
        createdAt: "2026-01-01T00:00:00Z",
        updatedAt: "2026-01-01T00:00:00Z",
      },
      turns: [],
      pendingCheckpoint: null,
    },
  };
}

function renderPage() {
  return render(
    <ConfigProvider locale={zhCN}>
      <App>
        <MemoryRouter
          initialEntries={[{ pathname: "/research/s1", state: { question: "供应商 360° 全景" } }]}
        >
          <Routes>
            <Route path="/research/:id" element={<ResearchSessionPage />} />
          </Routes>
        </MemoryRouter>
      </App>
    </ConfigProvider>,
  );
}

describe("ResearchSessionPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ token: "test-jwt" });
    useResearchStore.getState().reset();
    httpMock.get.mockResolvedValue(makeDetail());
    httpMock.post.mockResolvedValue({ data: { sessionId: "s1", turnId: "t1", status: "running" } });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("streaming 期间渲染由 events 派生的实时进度", async () => {
    stubOpenFetch(
      'event: research.connected\ndata: {"sessionId":"s1"}\n\n',
      'event: research.step.sql\ndata: {"stepIndex":0}\n\n',
      'event: research.finding\ndata: {"claim":"结论"}\n\n',
    );

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("执行查询")).toBeInTheDocument();
      expect(screen.getByText("得出结论")).toBeInTheDocument();
    });
  });

  it("首轮问题在流建立（onOpen）之后才提交", async () => {
    let resolveFetch!: (value: unknown) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
      ),
    );

    renderPage();

    // openSession 已完成，但流尚未建立（fetch 未 resolve）→ 首轮不得提前提交。
    await waitFor(() => expect(httpMock.get).toHaveBeenCalled());
    expect(httpMock.post).not.toHaveBeenCalled();

    resolveFetch({
      ok: true,
      body: openStream('event: research.connected\ndata: {"sessionId":"s1"}\n\n'),
    });

    await waitFor(() =>
      expect(httpMock.post).toHaveBeenCalledWith("/research/sessions/s1/turns", {
        question: "供应商 360° 全景",
      }),
    );
  });

  it("首轮提交失败显示错误且不产生未处理拒绝", async () => {
    stubOpenFetch('event: research.connected\ndata: {"sessionId":"s1"}\n\n');
    httpMock.post.mockRejectedValue(new Error("提交失败"));

    renderPage();

    await waitFor(() => expect(screen.getByText("提交失败")).toBeInTheDocument());
  });

  it("会话页显示本次研究的数据源名", async () => {
    httpMock.get.mockImplementation((url: string) => {
      if (url === "/datasources") {
        return Promise.resolve({ data: [{ id: 7, name: "THBI Oracle", isDefault: true }] });
      }
      const detail = makeDetail();
      // 不可变构造：不改 makeDetail() 的返回值本身。
      return Promise.resolve({
        data: { ...detail.data, session: { ...detail.data.session, datasourceId: 7 } },
      });
    });

    renderPage();

    expect(await screen.findByText("THBI Oracle")).toBeInTheDocument();
  });

  // B5 修复：answer 提交后页面必须把 store.resolutionInFlight 透传给 CheckpointCard，
  // 否则 store 已置 true 但卡片不显示「处理中…」Spin，用户看到「点完毫无反应」假象。
  it("answer 提交中：把 store.resolutionInFlight 透传给 CheckpointCard，渲染「处理中…」", async () => {
    // 准备一个 hypothesis 检查点：openSession 返回的 detail 把它带进 store。
    const hypothesisCheckpoint = {
      id: "cp-1",
      phase: "hypothesis",
      status: "pending",
      options: {
        arms: { metrics: [], conflicts: [] },
        candidates: [
          { statement: "假设 A", driver: "K1" },
          { statement: "假设 B", driver: "K2" },
        ],
      },
      prompt: "选要验证的",
      userChoice: null,
      decidedAt: null,
    };
    httpMock.get.mockResolvedValue({
      data: {
        session: {
          id: "s1",
          title: "研究",
          mode: "research",
          status: "running",
          question: "q",
          createdAt: "2026-01-01T00:00:00Z",
          updatedAt: "2026-01-01T00:00:00Z",
        },
        turns: [],
        pendingCheckpoint: hypothesisCheckpoint,
      },
    });

    renderPage();

    // 等卡片渲染
    await waitFor(() => expect(screen.getByText("假设 A")).toBeInTheDocument());

    // 模拟 answer 已提交但下一个 SSE 事件尚未到达的瞬间：把 resolutionInFlight=true
    // 直接打到 store 上（这是 answer 成功路径的 store 副作用）。
    useResearchStore.setState({ resolutionInFlight: true });

    // 卡片必须显示「处理中…」Spin（CheckpointCard 第 486 行）——证明 prop 已透传。
    expect(await screen.findByText("处理中…")).toBeInTheDocument();
  });

  // 详情 Drawer 替换轮（2026-10-08）：点「查看」按钮应打开右侧 Drawer，渲染该轮的
  // 完整内容。user 轮原样展示 question（含换行）；checkpoint 轮结构化展示
  // phase / status / prompt / options.candidates 全部。
  it("点 user 轮查看按钮打开 Drawer，原样渲染 question 全文", async () => {
    const longQuestion = "分步分析：为什么收货量下降？\n第一步统计最近 12 个月每月的收货量趋势；\n第二步按供应商拆分最近 3 个月收货量并对比找出降幅最大的供应商。";
    httpMock.get.mockResolvedValue({
      data: {
        session: {
          id: "s1",
          title: "研究",
          mode: "research",
          status: "running",
          question: longQuestion,
          createdAt: "2026-01-01T00:00:00Z",
          updatedAt: "2026-01-01T00:00:00Z",
        },
        turns: [{
          id: "t1",
          turnIndex: 0,
          role: "user",
          content: { question: longQuestion },
          createdAt: "2026-10-04T00:00:00Z",
        }],
        pendingCheckpoint: null,
      },
    });

    renderPage();

    // 触发 user 轮渲染（用 startsWith 避开 getByText 的全等匹配 —— <p> 文本带
    // \n 时 testing-library 把它和换行后的子节点一起视为多元素）。
    await waitFor(() =>
      expect(
        screen.getByText((content) => content.startsWith("分步分析")),
      ).toBeInTheDocument(),
    );
    // 点查看
    fireEvent.click(screen.getByTestId("detail-turn-t1"));
    // Drawer 打开 —— role=dialog 出现
    const dialog = await screen.findByRole("dialog");
    // 全文（带 \n）必须出现在 Drawer body 中，原样保留 —— 用 function matcher
    // 避免 getByText 全等对换行的处理。
    expect(
      within(dialog).getByText((content) => content.startsWith("分步分析")),
    ).toBeInTheDocument();
    // \n 也必须保留（whiteSpace: pre-wrap 在 Drawer body 上）
    expect(within(dialog).getByText((content) => content.includes("第一步统计"))).toBeInTheDocument();
  });

  it("点 checkpoint_awaiting 轮查看按钮打开 Drawer，结构化渲染 prompt + candidates[]", async () => {
    httpMock.get.mockResolvedValue({
      data: {
        session: {
          id: "s1",
          title: "研究",
          mode: "research",
          status: "awaiting_user",
          question: "q",
          createdAt: "2026-01-01T00:00:00Z",
          updatedAt: "2026-01-01T00:00:00Z",
        },
        turns: [{
          id: "t1",
          turnIndex: 0,
          role: "checkpoint_awaiting",
          content: {
            checkpointId: "cp-1",
            phase: "hypothesis",
            prompt: "共 3 条候选假设，请选择要验证的",
            options: {
              candidates: [
                { statement: "上游采购订单下达量收缩", driver: "K1" },
                { statement: "到货预约量减少", driver: "K2" },
                { statement: "收货执行环节变慢", driver: "K3" },
              ],
            },
          },
          createdAt: "2026-10-04T00:00:01Z",
        }],
        pendingCheckpoint: {
          id: "cp-1",
          phase: "hypothesis",
          status: "pending",
          options: {
            candidates: [
              { statement: "上游采购订单下达量收缩", driver: "K1" },
              { statement: "到货预约量减少", driver: "K2" },
              { statement: "收货执行环节变慢", driver: "K3" },
            ],
          },
          prompt: "共 3 条候选假设，请选择要验证的",
          userChoice: null,
          decidedAt: null,
        },
      },
    });

    renderPage();

    // 等候选渲染
    await waitFor(() => expect(screen.getByText("上游采购订单下达量收缩")).toBeInTheDocument());
    // 点查看
    fireEvent.click(screen.getByTestId("detail-turn-t1"));
    const dialog = await screen.findByRole("dialog");
    // prompt 全文 + 全部 3 条候选 statement 都必须出现在 Drawer body
    expect(within(dialog).getByText("共 3 条候选假设，请选择要验证的")).toBeInTheDocument();
    expect(within(dialog).getByText("上游采购订单下达量收缩")).toBeInTheDocument();
    expect(within(dialog).getByText("到货预约量减少")).toBeInTheDocument();
    expect(within(dialog).getByText("收货执行环节变慢")).toBeInTheDocument();
  });

  // 分析完成 notification：streaming=false + resolutionInFlight=false + !conflictError +
  // turns.length>0 时弹一次；提供"查看报告"按钮 navigate 到 /research/:id/report。
  describe("分析完成 notification", () => {
    // 用 querySelector 断言 DOM 出现 antd notification；生产代码用 App.useApp()
    // 拿到的实例，spy 静态 notification 拿不到，所以走 DOM 断言。
    const queryNotifications = () =>
      document.querySelectorAll(".ant-notification-notice");

    beforeEach(() => {
      // openSession 至少返回 1 个 turn（满足 turns.length>0 触发条件）。
      // store 字段用真实 useResearchStore（selector 风格），不允许替换 mockStoreState
      // —— 页面内有 11 个 useResearchStore((s) => s.*) 选择器，mock 整 store 会丢字段。
      httpMock.get.mockResolvedValue({
        data: {
          session: {
            id: "s1",
            title: "研究",
            mode: "research",
            status: "running",
            question: "q",
            createdAt: "2026-01-01T00:00:00Z",
            updatedAt: "2026-01-01T00:00:00Z",
          },
          turns: [
            {
              id: "t1",
              turnIndex: 0,
              role: "user",
              content: { question: "q" },
              createdAt: "2026-10-04T00:00:00Z",
            },
          ],
          pendingCheckpoint: null,
        },
      });
      // SSE 走裸 fetch；用不关闭的流占位，connectStream 拿到 body 后 onOpen
      // 触发但不退出循环 → streaming 保持 true，测试可以观察稳定状态。
      // （不写这个 stub 会让 connectStream 抛 fetch 失败，finally 把
      // streaming 置回 false，触发误判「完成」。）
      stubOpenFetch('event: research.connected\ndata: {"sessionId":"s1"}\n\n');
    });

    // 工具：等 useResearchSession 把页面流式编排完（openSession 把 turns 填上 +
    // connectStream 把 streaming 置 true）。React 18 自动批处理把 openSession 的
    // set(turns) 和 connectStream 的 set(streaming) 合到同一次 render —— 因此
    // 组件从未观察到「turns 满 + streaming=false」的中间态，避免误弹。
    async function waitForStreamingSettled() {
      await waitFor(() => {
        const state = useResearchStore.getState();
        expect(state.streaming).toBe(true);
        expect(state.turns.length).toBeGreaterThan(0);
      });
    }

    it("events 追加 research.done 事件时弹一次完成通知，按钮文案 = 查看报告", async () => {
      renderPage();
      await waitForStreamingSettled();
      // 此刻 events 里只有 connected 帧，未弹
      expect(queryNotifications()).toHaveLength(0);

      // 注入 research.done 事件
      act(() => {
        useResearchStore.setState((s) => ({
          events: [
            ...s.events,
            { name: "research.done", payload: {} } as never,
          ],
          streaming: false,
        }));
      });

      await waitFor(() => expect(queryNotifications()).toHaveLength(1));

      const notice = queryNotifications()[0];
      expect(notice).toHaveTextContent("分析已完成");
      expect(notice).toHaveTextContent("查看报告");
      // 6s 自动关闭（duration=6）+ 右上角
      const holder = notice.closest(".ant-notification");
      expect(holder?.className ?? "").toContain("ant-notification-topRight");

      // 再 render 一次，useRef 必须拦住重复弹
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(queryNotifications()).toHaveLength(1);
    });

    it("events 末尾不是 research.done 时不弹通知", async () => {
      renderPage();
      await waitForStreamingSettled();

      // 注入非 done 事件（如 progress）
      act(() => {
        useResearchStore.setState((s) => ({
          events: [
            ...s.events,
            { name: "research.progress", payload: { stage: "executing" } } as never,
          ],
        }));
      });

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(queryNotifications()).toHaveLength(0);
    });

    it("resolutionInFlight=true 时不弹通知（避免在 checkpoint 答完等待中误判完成）", async () => {
      renderPage();
      await waitForStreamingSettled();
      act(() => {
        useResearchStore.setState({ resolutionInFlight: true });
      });

      act(() => {
        useResearchStore.setState((s) => ({
          events: [
            ...s.events,
            { name: "research.done", payload: {} } as never,
          ],
          streaming: false,
        }));
      });

      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(queryNotifications()).toHaveLength(0);
    });

    it("conflictError + 同帧 done：toast 消费 conflict 后弹完成通知（真实业务流）", async () => {
      // 真实业务：conflictError 来了 → 页面 toast 显示 → 立即 clearConflictError
      // （B3.5 一次性消费）→ 紧接着 SSE 发 done 事件 → 此时 conflictError=null
      // → 应当弹"分析完成"通知。这是合理 UX：冲突已处置，分析真的结束了，
      // 引导用户去看报告。修这条断言时不要绕过 toast clear。
      renderPage();
      await waitForStreamingSettled();
      act(() => {
        useResearchStore.setState({
          conflictError: "并发检查点冲突：另一请求已先一步决/过期，请刷新后重试",
        });
      });

      act(() => {
        useResearchStore.setState((s) => ({
          events: [
            ...s.events,
            { name: "research.done", payload: {} } as never,
          ],
          streaming: false,
        }));
      });

      await waitFor(() => expect(queryNotifications()).toHaveLength(1));
    });

    // 真实用户场景：用户进一个**已存在**的 session（无 location.state.question），
    // useResearchSession 不 submitTurn，SSE 帧里包含 research.done。
    it("SSE 流里收到 research.done 帧：通知能弹", async () => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue({
          ok: true,
          body: openStream(
            'event: research.connected\ndata: {"sessionId":"s1"}\n\n',
            'event: research.done\ndata: {}\n\n',
          ),
        }),
      );

      render(
        <ConfigProvider locale={zhCN}>
          <App>
            <MemoryRouter initialEntries={[{ pathname: "/research/s1" }]}>
              <Routes>
                <Route path="/research/:id" element={<ResearchSessionPage />} />
              </Routes>
            </MemoryRouter>
          </App>
        </ConfigProvider>,
      );

      // 等 done 帧被 events 数组吸收
      await waitFor(
        () => {
          const s = useResearchStore.getState();
          expect(s.events.some((e) => e.name === "research.done")).toBe(true);
        },
        { timeout: 5000 },
      );

      await waitFor(() => expect(queryNotifications()).toHaveLength(1));
    });
  });
});
