import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// REST 走 httpClient（api/research.ts + researchStore 的 REST 路径共用一个 mock）。
const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock, apiClient: httpMock }));

import {
  answerCheckpoint,
  createResearchSession,
  getReport,
  getResearchSession,
  listReports,
  listResearchSessions,
  openResearchStream,
  submitTurn,
} from "../api/research";
import { useResearchStore, applyResearchEvent, buildInitialState } from "../stores/researchStore";
import { useAuthStore } from "../stores/authStore";
import { DEFAULT_TENANT_ID } from "../config";
import type { ResearchCheckpoint, ResearchSession, ResearchSseEvent } from "../types/research";

function sseStream(...frames: string[]): ReadableStream<Uint8Array> {
  const text = frames.join("");
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(text));
      controller.close();
    },
  });
}

function chunkedStream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

// 返回一个会随 signal 中止而 error 的流（模拟真实 fetch body 对 abort 的响应），
// 用于测 connectStream 的断流路径（mock fetch 默认不会把 signal 接到我们造的流上）。
function stubAbortableStream(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
      const signal = init?.signal as AbortSignal | undefined;
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          const fail = () => controller.error(new Error("aborted"));
          if (signal?.aborted) {
            fail();
            return;
          }
          signal?.addEventListener("abort", fail, { once: true });
        },
      });
      return Promise.resolve({ ok: true, body });
    }),
  );
}

// 同 stubAbortableStream，但先 enqueue 若干帧后保持打开（不 close），
// 供测试在流未结束前观察事件级判定（如降级 error 不应把 streaming 置 false）。
function stubAbortableOpenStream(...frames: string[]): void {
  const encoder = new TextEncoder();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
      const signal = init?.signal as AbortSignal | undefined;
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(encoder.encode(frames.join("")));
          const fail = () => controller.error(new Error("aborted"));
          if (signal?.aborted) {
            fail();
            return;
          }
          signal?.addEventListener("abort", fail, { once: true });
        },
      });
      return Promise.resolve({ ok: true, body });
    }),
  );
}

function makeSession(overrides: Partial<ResearchSession> = {}): ResearchSession {
  return {
    id: "s1",
    title: "研究：供应商 360°",
    mode: "research",
    status: "running",
    question: "供应商 360° 全景",
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("api/research REST", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("createResearchSession POST /research/sessions 并返回会话", async () => {
    const session = makeSession();
    httpMock.post.mockResolvedValue({ data: session });

    const result = await createResearchSession({ question: "供应商 360° 全景", mode: "research" });

    expect(httpMock.post).toHaveBeenCalledWith("/research/sessions", {
      question: "供应商 360° 全景",
      mode: "research",
    });
    expect(result).toEqual(session);
  });

  it("listResearchSessions GET /research/sessions 返回列表", async () => {
    const sessions = [makeSession()];
    httpMock.get.mockResolvedValue({ data: sessions });

    await expect(listResearchSessions()).resolves.toEqual(sessions);
    expect(httpMock.get).toHaveBeenCalledWith("/research/sessions");
  });

  it("getResearchSession GET /research/sessions/{id}", async () => {
    const detail = { session: makeSession(), turns: [], pendingCheckpoint: null };
    httpMock.get.mockResolvedValue({ data: detail });

    await expect(getResearchSession("s1")).resolves.toEqual(detail);
    expect(httpMock.get).toHaveBeenCalledWith("/research/sessions/s1");
  });

  it("submitTurn POST /research/sessions/{id}/turns 携带 question", async () => {
    httpMock.post.mockResolvedValue({ data: { sessionId: "s1", turnId: "t1", status: "running" } });

    const result = await submitTurn("s1", "再挖一层");

    expect(httpMock.post).toHaveBeenCalledWith("/research/sessions/s1/turns", {
      question: "再挖一层",
    });
    expect(result).toEqual({ sessionId: "s1", turnId: "t1", status: "running" });
  });

  it("answerCheckpoint POST /research/checkpoints/{id}/answer 携带 action + choice", async () => {
    httpMock.post.mockResolvedValue({ data: { sessionStatus: "running", nextPhase: "planning" } });

    const result = await answerCheckpoint("cp1", { action: "confirm", choice: { arm: 0 } });

    expect(httpMock.post).toHaveBeenCalledWith("/research/checkpoints/cp1/answer", {
      action: "confirm",
      choice: { arm: 0 },
    });
    expect(result).toEqual({ sessionStatus: "running", nextPhase: "planning" });
  });

  it("getReport GET /research/sessions/{id}/report（指定版本时带 version 参数）", async () => {
    const report = {
      id: "r1",
      version: 2,
      status: "published",
      payload: {},
      renderedMd: "# 报告",
      createdAt: "2026-01-01T00:00:00Z",
    };
    httpMock.get.mockResolvedValue({ data: report });

    await getReport("s1");
    expect(httpMock.get).toHaveBeenCalledWith("/research/sessions/s1/report", {
      params: undefined,
    });

    await getReport("s1", 2);
    expect(httpMock.get).toHaveBeenCalledWith("/research/sessions/s1/report", {
      params: { version: 2 },
    });
  });

  it("listReports GET /research/sessions/{id}/reports 返回版本列表", async () => {
    const reports = [{ id: "r1", version: 1, status: "published", createdAt: "2026-01-01T00:00:00Z" }];
    httpMock.get.mockResolvedValue({ data: reports });

    await expect(listReports("s1")).resolves.toEqual(reports);
    expect(httpMock.get).toHaveBeenCalledWith("/research/sessions/s1/reports");
  });
});

describe("api/research SSE（openResearchStream）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    useAuthStore.setState({ token: null });
  });

  it("注入 SSOT auth headers（Authorization + X-Tenant-Id）", async () => {
    useAuthStore.setState({ token: "test-jwt" });
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body: sseStream() });
    vi.stubGlobal("fetch", fetchMock);

    await openResearchStream("s1", () => {});

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/research/stream?sessionId=s1");
    expect(init.headers).toMatchObject({
      Authorization: "Bearer test-jwt",
      "X-Tenant-Id": DEFAULT_TENANT_ID,
    });
  });

  it("按 event: 名分发事件（顺序与名称都正确）", async () => {
    const frames = [
      'event: research.connected\ndata: {"sessionId":"s1"}\n\n',
      'event: research.intent\ndata: {"intent":"research"}\n\n',
      'event: research.done\ndata: {"degraded":false}\n\n',
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: sseStream(...frames) }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received.map((e) => e.name)).toEqual([
      "research.connected",
      "research.intent",
      "research.done",
    ]);
    expect(received[0].payload).toEqual({ sessionId: "s1" });
  });

  it("跳过心跳帧（行首 :）不解析为 JSON", async () => {
    const stream = sseStream(
      ': ping\n\n',
      'event: research.esl\ndata: {"metrics":[]}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received).toHaveLength(1);
    expect(received[0].name).toBe("research.esl");
  });

  it("跳过未知事件名", async () => {
    const stream = sseStream(
      'event: research.unknown\ndata: {"x":1}\n\n',
      'event: research.intent\ndata: {"intent":"research"}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received.map((e) => e.name)).toEqual(["research.intent"]);
  });

  it("兼容 CRLF 换行的帧分隔", async () => {
    const stream = sseStream(
      'event: research.intent\r\ndata: {"intent":"research"}\r\n\r\n',
      'event: research.done\r\ndata: {"degraded":false}\r\n\r\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received.map((e) => e.name)).toEqual(["research.intent", "research.done"]);
  });

  it("处理分帧边界：单帧跨多个 chunk 也能完整解析", async () => {
    const stream = chunkedStream([
      "event: research.intent\nda",
      'ta: {"intent":"research"}\n\n',
    ]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received).toHaveLength(1);
    expect(received[0]).toEqual({ name: "research.intent", payload: { intent: "research" } });
  });

  it("多行 data: 按 \\n 连接后解析", async () => {
    const stream = sseStream(
      'event: research.finding\ndata: {"a":\ndata: 1}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received).toHaveLength(1);
    expect(received[0].payload).toEqual({ a: 1 });
  });

  it("非法 JSON 帧被跳过", async () => {
    const stream = sseStream(
      'event: research.intent\ndata: not-json\n\n',
      'event: research.done\ndata: {"degraded":false}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received.map((e) => e.name)).toEqual(["research.done"]);
  });

  it("非对象 payload（data: 42）被跳过", async () => {
    const stream = sseStream(
      'event: research.intent\ndata: 42\n\n',
      'event: research.done\ndata: {"degraded":false}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received.map((e) => e.name)).toEqual(["research.done"]);
  });

  it("HTTP 非 200 抛错", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 403 }));
    await expect(openResearchStream("s1", () => {})).rejects.toThrow("HTTP 403");
  });

  it("无 body 抛错", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, body: null }));
    await expect(openResearchStream("s1", () => {})).rejects.toThrow("HTTP 200");
  });
});

describe("researchStore", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ token: null });
    useResearchStore.getState().reset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("answer 提交决策后 pendingCheckpoint 清空", async () => {
    useResearchStore.setState({
      pendingCheckpoint: {
        id: "cp1",
        phase: "intent",
        status: "pending",
        options: { prompt: "范围是否确认？" },
        prompt: "范围是否确认？",
        userChoice: null,
        decidedAt: null,
      },
    });
    httpMock.post.mockResolvedValue({ data: { sessionStatus: "running", nextPhase: "planning" } });

    await useResearchStore.getState().answer("cp1", "confirm", { arm: 0 });

    expect(httpMock.post).toHaveBeenCalledWith("/research/checkpoints/cp1/answer", {
      action: "confirm",
      choice: { arm: 0 },
    });
    expect(useResearchStore.getState().pendingCheckpoint).toBeNull();
  });

  it("connectStream events 只追加、不改写旧数组", async () => {
    const stream = sseStream(
      'event: research.intent\ndata: {"intent":"research"}\n\n',
      'event: research.esl\ndata: {"metrics":[]}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const refs: ResearchSseEvent[][] = [];
    const unsub = useResearchStore.subscribe((s) => refs.push(s.events));

    await useResearchStore.getState().connectStream("s1");
    unsub();

    const withOne = refs.find((e) => e.length === 1);
    const withTwo = refs.find((e) => e.length === 2);
    expect(withOne).toBeDefined();
    expect(withTwo).toBeDefined();
    expect(withOne!.length).toBe(1); // 旧数组引用未被追加
    expect(withTwo![0]).toBe(withOne![0]); // 同一事件对象，未 clone
    expect(withTwo).not.toBe(withOne); // 追加产生新数组
    expect(useResearchStore.getState().events).toHaveLength(2);
  });

  it("connectStream 把 research.checkpoint 落到 pendingCheckpoint", async () => {
    const stream = sseStream(
      'event: research.checkpoint\ndata: {"checkpointId":"cp1","phase":"intent","prompt":"范围是否确认？","options":{"prompt":"范围是否确认？"}}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    const state = useResearchStore.getState();
    expect(state.pendingCheckpoint).toMatchObject({
      id: "cp1",
      phase: "intent",
      status: "pending",
      prompt: "范围是否确认？",
      userChoice: null,
      decidedAt: null,
    });
    expect(state.events[0].name).toBe("research.checkpoint");
  });

  it("research.done 收尾：streaming 置 false", async () => {
    const stream = sseStream('event: research.done\ndata: {"degraded":false}\n\n');
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    expect(useResearchStore.getState().streaming).toBe(false);
  });

  it("降级 research.error（uiHint:degraded）不关流，后续 done 仍落地", async () => {
    const stream = sseStream(
      'event: research.error\ndata: {"code":"llm_unavailable","message":"无可用 LLM","uiHint":"degraded"}\n\n',
      'event: research.done\ndata: {"degraded":true}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    const state = useResearchStore.getState();
    expect(state.events.map((e) => e.name)).toEqual(["research.error", "research.done"]);
    expect(state.error).toBeNull(); // 降级不置 error
    expect(state.streaming).toBe(false);
  });

  it("终态 research.error（uiHint:terminal）关流并落地 error", async () => {
    const stream = sseStream(
      'event: research.error\ndata: {"code":"turn_failed","message":"boom","uiHint":"terminal"}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    const state = useResearchStore.getState();
    expect(state.streaming).toBe(false);
    expect(state.error).toBe("boom");
  });

  it("判定读 uiHint 而非 code：未知 code 但 uiHint:terminal 也关流", async () => {
    const stream = sseStream(
      'event: research.error\ndata: {"code":"some_future_code","message":"z","uiHint":"terminal"}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    const state = useResearchStore.getState();
    expect(state.streaming).toBe(false);
    expect(state.error).toBe("z");
  });

  it("判定读 uiHint 而非 code：turn_failed 但 uiHint:degraded 保持打开、不落 error", async () => {
    const stream = sseStream(
      'event: research.error\ndata: {"code":"turn_failed","message":"w","uiHint":"degraded"}\n\n',
      'event: research.done\ndata: {"degraded":true}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    const state = useResearchStore.getState();
    expect(state.events.map((e) => e.name)).toEqual(["research.error", "research.done"]);
    expect(state.error).toBeNull(); // 不按 code 判断，degraded 不落 error
  });

  it("sendQuestion 创建会话 + 提交首轮 + 返回 sessionId", async () => {
    const session = makeSession();
    httpMock.post
      .mockResolvedValueOnce({ data: session }) // createResearchSession
      .mockResolvedValueOnce({ data: { sessionId: "s1", turnId: "t1", status: "running" } }); // submitTurn

    const id = await useResearchStore.getState().sendQuestion("供应商 360° 全景");

    expect(id).toBe("s1");
    expect(useResearchStore.getState().currentSession).toEqual(session);
    expect(httpMock.post).toHaveBeenNthCalledWith(1, "/research/sessions", {
      question: "供应商 360° 全景",
      mode: "research",
    });
    expect(httpMock.post).toHaveBeenNthCalledWith(2, "/research/sessions/s1/turns", {
      question: "供应商 360° 全景",
    });
  });

  it("openSession 载入会话详情（session/turns/pendingCheckpoint）", async () => {
    const detail = {
      session: makeSession({ status: "awaiting_user" }),
      turns: [{ id: "t1", turnIndex: 0, role: "user", content: { question: "q" }, createdAt: "2026-01-01T00:00:00Z" }],
      pendingCheckpoint: null,
    };
    httpMock.get.mockResolvedValue({ data: detail });

    await useResearchStore.getState().openSession("s1");

    expect(useResearchStore.getState().currentSession).toEqual(detail.session);
    expect(useResearchStore.getState().turns).toEqual(detail.turns);
    expect(useResearchStore.getState().pendingCheckpoint).toBeNull();
  });

  it("loadSessions 载入会话列表", async () => {
    const sessions = [makeSession()];
    httpMock.get.mockResolvedValue({ data: sessions });

    await useResearchStore.getState().loadSessions();

    expect(useResearchStore.getState().sessions).toEqual(sessions);
    expect(useResearchStore.getState().sessionsLoading).toBe(false);
  });

  it("loadReport 载入报告", async () => {
    const report = {
      id: "r1",
      version: 1,
      status: "published",
      payload: {},
      renderedMd: "# 报告",
      createdAt: "2026-01-01T00:00:00Z",
    };
    httpMock.get.mockResolvedValue({ data: report });

    await useResearchStore.getState().loadReport("s1");

    expect(useResearchStore.getState().report).toEqual(report);
  });

  it("loadReports 载入报告版本列表", async () => {
    const reports = [{ id: "r1", version: 1, status: "published", createdAt: "2026-01-01T00:00:00Z" }];
    httpMock.get.mockResolvedValue({ data: reports });

    await useResearchStore.getState().loadReports("s1");

    expect(useResearchStore.getState().reports).toEqual(reports);
  });

  it("loadSessions 失败落 error", async () => {
    httpMock.get.mockRejectedValue(new Error("boom"));
    await useResearchStore.getState().loadSessions();
    expect(useResearchStore.getState().error).toBe("boom");
    expect(useResearchStore.getState().sessionsLoading).toBe(false);
  });

  it("loadSessions 非 Error 拒绝也落 error（String(err) 分支）", async () => {
    httpMock.get.mockRejectedValue("boom-string");
    await useResearchStore.getState().loadSessions();
    expect(useResearchStore.getState().error).toBe("boom-string");
  });

  it("openSession 失败落 error", async () => {
    httpMock.get.mockRejectedValue(new Error("boom"));
    await useResearchStore.getState().openSession("s1");
    expect(useResearchStore.getState().error).toBe("boom");
  });

  it("deleteSession 失败：抛出但不写共享 error 槽（反馈归列表页，防串页）", async () => {
    httpMock.delete.mockRejectedValue(new Error("boom"));
    useResearchStore.setState({ error: "stale" }); // 预置陈旧错误，验证动作先清槽

    await expect(useResearchStore.getState().deleteSession("s1")).rejects.toThrow("boom");

    expect(httpMock.delete).toHaveBeenCalledWith("/research/sessions/s1");
    expect(useResearchStore.getState().error).toBeNull();
  });

  it("sendQuestion 创建会话失败时抛出并落 error", async () => {
    httpMock.post.mockRejectedValue(new Error("boom"));
    await expect(useResearchStore.getState().sendQuestion("q")).rejects.toThrow("boom");
    expect(useResearchStore.getState().error).toBe("boom");
  });

  it("submitTurn 失败时抛出并落 error", async () => {
    httpMock.post.mockRejectedValue(new Error("boom"));
    await expect(useResearchStore.getState().submitTurn("s1", "q")).rejects.toThrow("boom");
    expect(useResearchStore.getState().error).toBe("boom");
  });

  it("answer 失败时抛出，pendingCheckpoint 不清空", async () => {
    const pending: ResearchCheckpoint = {
      id: "cp1",
      phase: "intent",
      status: "pending",
      options: {},
      prompt: "p",
      userChoice: null,
      decidedAt: null,
    };
    useResearchStore.setState({ pendingCheckpoint: pending });
    httpMock.post.mockRejectedValue(new Error("boom"));

    await expect(useResearchStore.getState().answer("cp1", "confirm")).rejects.toThrow("boom");

    expect(useResearchStore.getState().pendingCheckpoint).toEqual(pending);
  });

  it("answer 不带 choice 时默认提交空对象", async () => {
    httpMock.post.mockResolvedValue({ data: { sessionStatus: "running", nextPhase: "planning" } });

    await useResearchStore.getState().answer("cp1", "reject");

    expect(httpMock.post).toHaveBeenCalledWith("/research/checkpoints/cp1/answer", {
      action: "reject",
      choice: {},
    });
    expect(useResearchStore.getState().pendingCheckpoint).toBeNull();
  });

  it("loadReport 失败落 error", async () => {
    httpMock.get.mockRejectedValue(new Error("boom"));
    await useResearchStore.getState().loadReport("s1");
    expect(useResearchStore.getState().error).toBe("boom");
  });

  it("loadReports 失败落 error", async () => {
    httpMock.get.mockRejectedValue(new Error("boom"));
    await useResearchStore.getState().loadReports("s1");
    expect(useResearchStore.getState().error).toBe("boom");
  });

  it("research.checkpoint 载荷字段非法时补默认值", async () => {
    const stream = sseStream(
      'event: research.checkpoint\ndata: {"checkpointId":1,"phase":"bogus","prompt":7,"options":[1,2]}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    const cp = useResearchStore.getState().pendingCheckpoint!;
    expect(cp.id).toBe("");
    expect(cp.phase).toBe("intent");
    expect(cp.prompt).toBe("");
    expect(cp.options).toEqual({});
  });

  it("终态 uiHint:terminal 无 message 时 error 回退为 research.error", async () => {
    const stream = sseStream(
      'event: research.error\ndata: {"code":"turn_failed","uiHint":"terminal"}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    expect(useResearchStore.getState().error).toBe("research.error");
    expect(useResearchStore.getState().streaming).toBe(false);
  });

  it("uiHint 字段缺失时走降级分支（保持打开、不落 error）", async () => {
    const stream = sseStream(
      'event: research.error\ndata: {"code":"turn_failed","message":"legacy"}\n\n',
      'event: research.done\ndata: {"degraded":true}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    const state = useResearchStore.getState();
    expect(state.events.map((e) => e.name)).toEqual(["research.error", "research.done"]);
    expect(state.error).toBeNull(); // 未知/旧 payload 防御：缺 uiHint 不按终态处理
  });

  it("降级 error（uiHint:degraded）不把 streaming 置 false（流未结束时观察）", async () => {
    stubAbortableOpenStream(
      'event: research.error\ndata: {"code":"step_failed","message":"y","uiHint":"degraded"}\n\n',
    );
    const p = useResearchStore.getState().connectStream("s1");

    // 等 degraded 事件被应用（流保持打开，尚未结束）
    await vi.waitFor(() => expect(useResearchStore.getState().events).toHaveLength(1));
    expect(useResearchStore.getState().streaming).toBe(true);
    expect(useResearchStore.getState().error).toBeNull();

    useResearchStore.getState().reset();
    await p;
  });

  it("connectStream 网络错误（非断流）落 error 并收流", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));

    await useResearchStore.getState().connectStream("s1");

    expect(useResearchStore.getState().error).toBe("network down");
    expect(useResearchStore.getState().streaming).toBe(false);
  });

  it("connectStream 主动断流（reset）不落 error", async () => {
    stubAbortableStream();
    const p = useResearchStore.getState().connectStream("s1");

    expect(useResearchStore.getState().streaming).toBe(true);
    useResearchStore.getState().reset();
    await p;

    expect(useResearchStore.getState().error).toBeNull();
    expect(useResearchStore.getState().streaming).toBe(false);
  });

  it("connectStream 外部 signal 中止后断流且不落 error", async () => {
    stubAbortableStream();
    const ac = new AbortController();
    const p = useResearchStore.getState().connectStream("s1", ac.signal);

    expect(useResearchStore.getState().streaming).toBe(true);
    ac.abort();
    await p;

    expect(useResearchStore.getState().error).toBeNull();
    expect(useResearchStore.getState().streaming).toBe(false);
  });

  it("connectStream 重复调用断掉旧流（activeAbort 非空分支）", async () => {
    stubAbortableStream();
    const p1 = useResearchStore.getState().connectStream("s1");
    const p2 = useResearchStore.getState().connectStream("s2"); // 同步断掉 p1 的旧流

    await p1; // 旧流因被断而收敛，不落 error
    expect(useResearchStore.getState().error).toBeNull();

    useResearchStore.getState().reset();
    await p2;
    expect(useResearchStore.getState().error).toBeNull();
  });

  it("connectStream 流建立后调用 onOpen（开流成功即通知一次）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        body: sseStream('event: research.connected\ndata: {"sessionId":"s1"}\n\n'),
      }),
    );
    const onOpen = vi.fn();

    await useResearchStore.getState().connectStream("s1", undefined, onOpen);

    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("connectStream 网络错误也调用 onOpen（避免串行调用方挂起）", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    const onOpen = vi.fn();

    await useResearchStore.getState().connectStream("s1", undefined, onOpen);

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(useResearchStore.getState().error).toBe("network down");
  });

  it("connectStream 主动断流也调用 onOpen 且只通知一次", async () => {
    stubAbortableStream();
    const onOpen = vi.fn();
    const p = useResearchStore.getState().connectStream("s1", undefined, onOpen);

    useResearchStore.getState().reset();
    await p;

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(useResearchStore.getState().error).toBeNull();
  });

  // B3.5: checkpoint_conflict branch
  it("uiHint=conflict 设置 error + conflictError 并关闭流", () => {
    const initial = buildInitialState();
    const event = {
      name: "research.error" as const,
      payload: { code: "checkpoint_conflict", message: "决策冲突", uiHint: "conflict" as const },
    };
    const next = applyResearchEvent(initial, event);
    expect(next.error).toBe("决策冲突");
    expect(next.conflictError).toBe("决策冲突");
    expect(next.streaming).toBe(false);
  });

  it("uiHint=conflict 无 message 时回退为 i18n key", () => {
    const initial = buildInitialState();
    const event = {
      name: "research.error" as const,
      payload: { code: "checkpoint_conflict", uiHint: "conflict" as const },
    };
    const next = applyResearchEvent(initial, event);
    expect(next.error).toBe("research.error.checkpoint_conflict");
    expect(next.conflictError).toBe("research.error.checkpoint_conflict");
    expect(next.streaming).toBe(false);
  });

  it("uiHint=degraded 保持流打开（现有行为不变）", () => {
    const initial = buildInitialState();
    const event = {
      name: "research.error" as const,
      payload: { code: "step_failed", uiHint: "degraded" as const },
    };
    const next = applyResearchEvent(initial, event);
    expect(next.error).toBeUndefined();
    expect(next.conflictError).toBeUndefined();
    expect(next.streaming).toBeUndefined();
  });
});
