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

    // ResearchProgress 现在在右侧 timeline Drawer 内（live 进度挪走，UX 与
    // 用户显式请求一致）。先打开 Drawer 才能观察实时进度。
    fireEvent.click(screen.getByRole("button", { name: /时间线/ }));

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

  // UX 修复：长标题（如「分步分析：今年采购订单的一个趋势情况…」）把 header 撑满
  // 影响主内容。改成 header 只有「会话信息」按钮 + 右侧 Drawer 展示完整标题 +
  // 元数据。
  it("header 不直接渲染长标题，点会话信息按钮打开右侧 Drawer 展示完整标题", async () => {
    const longTitle =
      "分步分析：今年采购订单的一个趋势情况，从基地、供应商、物料等几个维度给出结论";
    httpMock.get.mockResolvedValue({
      data: {
        session: {
          id: "s1",
          title: longTitle,
          mode: "research",
          status: "running",
          question: "原始问题：今年采购订单趋势？",
          createdAt: "2026-10-01T00:00:00Z",
          updatedAt: "2026-10-09T00:00:00Z",
        },
        turns: [],
        pendingCheckpoint: null,
      },
    });

    renderPage();

    // header 不应有这个长标题（被搬走了）
    expect(screen.queryByText(longTitle)).not.toBeInTheDocument();

    // 但有触发按钮
    const trigger = await screen.findByRole("button", { name: /会话信息/ });
    fireEvent.click(trigger);

    // Drawer 打开后长标题在 dialog 内
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(longTitle)).toBeInTheDocument();
    // 元数据也都展示
    expect(within(dialog).getByText("原始问题：今年采购订单趋势？")).toBeInTheDocument();
  });

  // UX 修复：多步研究的 turn 序列（解析意图/生成执行步骤/开始步骤/执行查询/...
  // 20+ 条）渲染为长竖直列表，占主内容 1/4 高度。挪到右侧 Drawer，header 加
  // 「时间线 (N 步)」按钮，N 来自 turns.length。
  it("主内容区不直接渲染完整时间线，点时间线按钮打开 Drawer 展示，header 按钮文案带步数", async () => {
    const manyTurns = [
      {
        id: "t1",
        turnIndex: 0,
        role: "agent",
        content: { text: "解析意图 UNIQUEMARKER_INTENT" },
        createdAt: "2026-10-04T00:00:00Z",
      },
      {
        id: "t2",
        turnIndex: 1,
        role: "agent",
        content: { text: "生成执行步骤 UNIQUEMARKER_STEPS" },
        createdAt: "2026-10-04T00:00:01Z",
      },
      {
        id: "t3",
        turnIndex: 2,
        role: "agent",
        content: { text: "制定研究计划 UNIQUEMARKER_PLAN" },
        createdAt: "2026-10-04T00:00:02Z",
      },
    ];
    httpMock.get.mockResolvedValue({
      data: {
        session: {
          id: "s1",
          title: "t",
          mode: "research",
          status: "running",
          question: "q",
          createdAt: "2026-10-01T00:00:00Z",
          updatedAt: "2026-10-09T00:00:00Z",
        },
        turns: manyTurns,
        pendingCheckpoint: null,
      },
    });

    renderPage();

    // header 按钮文案带步数（3 步）
    const trigger = await screen.findByRole("button", { name: /时间线.*3.*步/ });
    expect(trigger).toBeInTheDocument();

    // Drawer 未开时主内容不应有任何 turn 文本（关键不变量：未来谁把 timeline
    // 移回主内容区，测试立刻红）
    expect(screen.queryByText(/UNIQUEMARKER_INTENT/)).not.toBeInTheDocument();
    expect(screen.queryByText(/UNIQUEMARKER_STEPS/)).not.toBeInTheDocument();
    expect(screen.queryByText(/UNIQUEMARKER_PLAN/)).not.toBeInTheDocument();

    // 点按钮 → Drawer 打开
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/UNIQUEMARKER_INTENT/)).toBeInTheDocument();
    expect(within(dialog).getByText(/UNIQUEMARKER_STEPS/)).toBeInTheDocument();
    expect(within(dialog).getByText(/UNIQUEMARKER_PLAN/)).toBeInTheDocument();
  });

  // 历史问答（右侧 Drawer）：点 header 按钮 → 拉取 sessions 列表 → 渲染，
  // 当前会话标「当前」高亮；点其它会话关闭 Drawer 并 navigate。
  it("历史问答 Drawer：打开后拉 sessions 列表，标当前会话，点其它会话 navigate", async () => {
    // 第二次 listResearchSessions 调用会返回这俩会话（页面里点按钮时才发）。
    // useResearchStore 的 mock 是按调用顺序复用 httpMock.get；
    // ResearchSessionPage 走的是 listResearchSessions → GET /research/sessions
    // （httpClient.get 而不是裸 fetch，path 没有 /research/sessions/s1 那种 detail 形态）。
    const otherSession = {
      id: "s-other",
      title: "另一项研究：采购订单",
      mode: "research" as const,
      status: "done" as const,
      question: "上个月的采购订单量？",
      createdAt: "2026-10-08T00:00:00Z",
      updatedAt: "2026-10-08T00:00:00Z",
    };
    const currentInList = {
      id: "s1",
      title: "研究：供应商 360°",
      mode: "research" as const,
      status: "running" as const,
      question: "供应商 360° 全景",
      createdAt: "2026-10-09T00:00:00Z",
      updatedAt: "2026-10-09T00:00:00Z",
    };
    httpMock.get.mockImplementation((url: string) => {
      if (url === "/research/sessions") {
        return Promise.resolve({ data: [currentInList, otherSession] });
      }
      const detail = makeDetail();
      return Promise.resolve({
        data: { ...detail.data, session: { ...detail.data.session, id: "s1" } },
      });
    });

    renderPage();

    // 打开 Drawer
    const trigger = await screen.findByRole("button", { name: /历史问答/ });
    fireEvent.click(trigger);

    // Drawer 出现，里面有另一项研究的标题
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("另一项研究：采购订单")).toBeInTheDocument();
    // 当前会话 s1 也以「当前」标签标出来
    expect(within(dialog).getByText("当前")).toBeInTheDocument();

    // 点其它会话 → Drawer 关闭 + navigate 切到该会话
    fireEvent.click(within(dialog).getByTestId("history-session-s-other"));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });

  // 步数残留修复：openSession 失败 / 还没拉到会话时，store.turns 是上次会话的
  // 残留数据，按钮不应该展示误导性的「时间线 (9 步)」。条件：**currentSession
  // 已设置**才显示步数。
  it("currentSession=null 时（残留 turns 来自上次会话）：按钮不显示步数", async () => {
    // 模拟残留状态：openSession 失败 / 还没拉会话，但 store.turns 有上次会话数据
    act(() => {
      useResearchStore.setState({
        currentSession: null,
        turns: [
          { id: "stale-1", turnIndex: 0, role: "agent", content: { text: "stale" }, createdAt: "2026-10-01" },
          { id: "stale-2", turnIndex: 1, role: "agent", content: { text: "stale" }, createdAt: "2026-10-01" },
        ],
      });
    });

    renderPage();

    // 按钮存在但**不带**步数后缀（关键不变量：未来谁去掉 currentSession 条件
    // 立刻红）。accessible name 是「history 时间线」（icon label + text），
    // 用部分匹配。
    const trigger = screen.getByRole("button", { name: /时间线/ });
    expect(trigger).toBeInTheDocument();
    // 关键不变量：accessible name 里**不**含步数。
    expect(trigger).not.toHaveTextContent(/\d+\s*步/);
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

  // B5 Round 7 修复：处理中 banner 提到 ResearchSessionPage 外层，**不**随
  // CheckpointCard 的 key={pendingCheckpoint.id} 重挂载消失。变灰→变绿→下个
  // checkpoint 现象的根因就是 banner 当时在 card 里。
  it("pendingCheckpoint + resolutionInFlight=true：banner 在页面层（不在 CheckpointCard 内）", async () => {
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
        pendingCheckpoint: {
          id: "cp-1",
          phase: "hypothesis",
          status: "pending",
          options: { candidates: [{ statement: "假设 A", driver: "K1" }] },
          prompt: "选要验证的",
          userChoice: null,
          decidedAt: null,
        },
      },
    });

    renderPage();

    // 等卡片渲染完
    await waitFor(() => expect(screen.getByText("假设 A")).toBeInTheDocument());

    // 模拟 answer 已提交
    useResearchStore.setState({ resolutionInFlight: true });

    // banner 存在
    const banner = await screen.findByTestId("checkpoint-processing-banner");
    expect(banner).toBeInTheDocument();
    // 字号 ≥ 20（与设计要求一致）
    const style = window.getComputedStyle(banner);
    expect(parseInt(style.fontSize, 10)).toBeGreaterThanOrEqual(20);

    // banner **不在** CheckpointCard 内 —— 这是本轮 bug 修复的关键不变量。
    // CheckpointCard 渲染一个 antd Card，根节点带 .ant-card 类；banner 必须在它
    // 外面，否则 key 切换会把它一起重挂载。
    const card = banner?.closest(".ant-card");
    expect(card).toBeNull();
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

    // 时间线挪到了右侧 Drawer，先打开 Drawer 才能看到 turn 文本和「查看」按钮。
    fireEvent.click(screen.getByRole("button", { name: /时间线/ }));
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

    // 时间线挪到右侧 Drawer 后先打开
    fireEvent.click(screen.getByRole("button", { name: /时间线/ }));
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

    it("resolutionInFlight=true 但 done 帧到：通知弹（处理中 banner 与 done 通知正交）", async () => {
      // 2026-10-09 修复：trigger 不再用 `resolutionInFlight` 闸（该字段是 B5「处理中
      // banner + 按钮 disabled」用的 UI 状态，要随 answer 提交后一直保留到下一个
      // checkpoint 帧）。done 帧本身已是研究终态，与该字段正交，通知该弹。
      // 真实业务里 done 到达时 resolutionInFlight 必为 true（用户刚答完最后一题
      // → 中间帧不重置 → 一直 true 到 done），所以这个用例同时覆盖真实业务流。
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

      await waitFor(() => expect(queryNotifications()).toHaveLength(1));
    });

    // 真实业务流 bug 修复（2026-10-09）：用户答完最后一个检查点 → resolutionInFlight=true
    // （answer 路径 284 行）→ SSE 推 research.finding（applyResearchEvent default 分支）
    // → 推 research.report → 推 research.done。如果 reducer default 不重置
    // resolutionInFlight，trigger 闸永远 true，通知永远不弹。修复：default 分支显式
    // resolutionInFlight: false（任何中间帧都意味着上一题 answer 已被消化）。
    it("answer 后 SSE 推 finding/report/done：通知必须弹", async () => {
      renderPage();
      await waitForStreamingSettled();

      act(() => {
        useResearchStore.setState({ resolutionInFlight: true });
      });

      // **走真实 reducer**（store 实际路径：line 342 `set(state => applyResearchEvent(...))`）——
      // 直接 setState events 会绕过 reducer，看不到真实业务流。
      const { applyResearchEvent } = await import("../stores/researchStore");
      act(() => {
        useResearchStore.setState((s) =>
          applyResearchEvent(
            { ...s, events: [...s.events, { name: "research.finding", payload: { claim: "测试结论" } } as never] },
            { name: "research.finding", payload: { claim: "测试结论" } } as never,
          ),
        );
      });
      act(() => {
        useResearchStore.setState((s) =>
          applyResearchEvent(
            { ...s, events: [...s.events, { name: "research.report", payload: {} } as never] },
            { name: "research.report", payload: {} } as never,
          ),
        );
      });
      act(() => {
        useResearchStore.setState((s) =>
          applyResearchEvent(
            { ...s, events: [...s.events, { name: "research.done", payload: {} } as never] },
            { name: "research.done", payload: {} } as never,
          ),
        );
      });

      await waitFor(() => expect(queryNotifications()).toHaveLength(1));
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

      // 直接等通知弹出：done 帧被 reducer 处理后会触发 trigger useEffect 弹通知。
      // 不查 events 数组——useResearchSession 在 streaming 边沿会重拉会话
      // （researchStore.openSession 把 events 重置为 []），所以 events 里
      // 短时存在 done 也可能被立即清掉；通知一旦弹出就是终态。
      await waitFor(() => expect(queryNotifications()).toHaveLength(1), { timeout: 5000 });
    });
  });
});
