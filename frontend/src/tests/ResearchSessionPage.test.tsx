// ResearchSessionPage 页面级测试（feat-research-entry 修复轮）。
//
// 重点覆盖两条修复：
// 1. streaming 期间，页面由 store.events（SSE 累积事件）派生并渲染实时进度——
//    若进度视图没渲染任何东西，本测试会红（`getByText("执行查询")` 找不到）。
// 2. 首轮编排串行化：流建立（onOpen）之前不得提交首轮（后端不重放历史），
//    提交失败也不能产生未处理拒绝。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
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
});
