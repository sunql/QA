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
import { i18n } from "../i18n";
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

  // Task 12b：无游标（null/undefined）⇒ URL 与今天逐字节一致；有游标才追加。
  it("传 lastEventId 时 URL 追加 &lastEventId=<n>", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body: sseStream() });
    vi.stubGlobal("fetch", fetchMock);

    await openResearchStream("s1", () => {}, undefined, undefined, 7);

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/research/stream?sessionId=s1&lastEventId=7",
    );
  });

  it("lastEventId 为 null 时 URL 不带该参数（与旧行为逐字节一致）", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body: sseStream() });
    vi.stubGlobal("fetch", fetchMock);

    await openResearchStream("s1", () => {}, undefined, undefined, null);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/research/stream?sessionId=s1");
  });

  it("解析 id: 行（事件带上 seq）", async () => {
    const stream = sseStream(
      'id: 12\nevent: research.intent\ndata: {"intent":"research"}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received).toHaveLength(1);
    expect(received[0].id).toBe(12);
  });

  it("同帧多个 id: 行时最后一个生效", async () => {
    const stream = sseStream(
      'id: 1\nid: 2\nevent: research.intent\ndata: {"intent":"research"}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received).toHaveLength(1);
    expect(received[0].id).toBe(2);
  });

  it("同帧多个 id: 行，最后一个非整数 ⇒ id 为 undefined（不保留更早的有效值）", async () => {
    // SSE 规范「最后一个 id: 生效」的字面含义：后面的非法值必须**重置**，
    // 而不是被 continue 跳过后留下更早的 5。
    const stream = sseStream(
      'id: 5\nid: abc\nevent: research.intent\ndata: {"intent":"research"}\n\n',
      'id: 5\nid:\nevent: research.esl\ndata: {"metrics":[]}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received.map((e) => e.name)).toEqual(["research.intent", "research.esl"]);
    expect(received[0].id).toBeUndefined();
    expect(received[1].id).toBeUndefined();
  });

  it("id: 非数字 / 空值被忽略，事件仍照常分发（id 为 undefined）", async () => {
    const stream = sseStream(
      'id: abc\nevent: research.intent\ndata: {"intent":"research"}\n\n',
      'id:\nevent: research.esl\ndata: {"metrics":[]}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received.map((e) => e.name)).toEqual(["research.intent", "research.esl"]);
    expect(received[0].id).toBeUndefined();
    expect(received[1].id).toBeUndefined();
  });

  it("无 id: 的帧不写 id 键（既有帧断言 toEqual 不变形）", async () => {
    const stream = sseStream('event: research.intent\ndata: {"intent":"research"}\n\n');
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received[0]).toEqual({
      name: "research.intent",
      payload: { intent: "research" },
    });
    expect("id" in received[0]).toBe(false);
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

  // X4（1012 cross-refs spec §P0-X4）：跨 chunk 的 CRLF 不能被旧 consumeFrames
  // 的全局 replace 误归一化。旧实现在调用前对整个 buffer 做
  // `replace(/\r\n|\r/g, "\n")`，会：
  //   1) 把 data 里的 `\r` 抹成 `\n`，JSON parse 错位
  //   2) 当 \r 跨 chunk（chunk1 末尾是 \r，chunk2 开头是 \n）时，\r 被提前归一，
  //      下一次进入 buffer 的 \n 形成虚假的 \n\n 边界，把一帧切成两半
  // 现在的解析走共享 StreamChunkParser，\r 只在 event/data 行的**末尾**被剥离。
  it("X4：data 内的 \\r 跨 chunk 边界仍保留（不归一化到 \\n）", async () => {
    // 单帧 "event: ...\ndata: {\"x\": 1}\n\n" 但把数据中的 \r\n 跨 chunk：
    //   chunk1 = "...\ndata: {\"x\": 1\r"
    //   chunk2 = "\n}\n\n"
    // 旧实现：chunk1 末尾的 \r 被 replace 成 \n，与 chunk2 首个 \n 形成 \n\n
    // 边界 → 帧 1 = "event: ...\ndata: {\"x\": 1"（data 残缺）→ JSON.parse 抛错 → 跳帧。
    const stream = chunkedStream([
      'event: research.intent\ndata: {"x": 1\r',
      '\n}\n\n',
    ]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received).toHaveLength(1);
    expect(received[0].name).toBe("research.intent");
    // 关键：data 里的 \r 必须保留（与 chat 一致，用 stream_chunk_parser）
    expect(received[0].payload).toEqual({ x: 1 });
  });

  // X4（1012 cross-refs spec §P0-X4）：EOF 守门。旧 consumeFrames 在 EOF 时
  // 只把 buffer 当 remainder 返回，**不**强制把残留切为最后一帧，导致
  // 后端以非 \n\n 结尾的最后一条事件被静默丢弃（如 connection close 提前）。
  // 现在走 StreamChunkParser.flush() 强制吐残留。
  it("X4：最后一帧无 \\n\\n 终止也照常分发（EOF 守门）", async () => {
    const stream = chunkedStream([
      'event: research.done\ndata: {"degraded":false}',  // 无尾 \n\n
    ]);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: ResearchSseEvent[] = [];
    await openResearchStream("s1", (e) => received.push(e));

    expect(received).toHaveLength(1);
    expect(received[0]).toEqual({
      name: "research.done",
      payload: { degraded: false },
    });
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
    // Task 12b：store 会把最后收到的 seq 写进 sessionStorage（键按会话隔离），
    // 测试间必须清掉，否则上一条用例的游标会串进下一条的 URL。
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // B4: answer 提交后保留 pendingCheckpoint + resolutionInFlight 标志，等待下一个 SSE 事件再切换
  it("answer submit: pendingCheckpoint stays until next SSE event arrives", async () => {
    useResearchStore.setState({
      pendingCheckpoint: {
        id: "cp1",
        phase: "intent",
        status: "pending",
        options: {},
        prompt: "范围是否确认？",
        userChoice: null,
        decidedAt: null,
      },
    });
    httpMock.post.mockResolvedValue({ data: { sessionStatus: "running", nextPhase: "planning" } });

    await useResearchStore.getState().answer("cp1", "confirm", { arm: 0 });

    // API 调用正确
    expect(httpMock.post).toHaveBeenCalledWith("/research/checkpoints/cp1/answer", {
      action: "confirm",
      choice: { arm: 0 },
    });
    // B4: pendingCheckpoint 保留（不清空），resolutionInFlight 为 true
    const state = useResearchStore.getState();
    expect(state.pendingCheckpoint).not.toBeNull();
    expect(state.resolutionInFlight).toBe(true);
  });

  it("answer submit: next SSE checkpoint event clears both pendingCheckpoint and resolutionInFlight", async () => {
    // 模拟 answer 成功提交后、下一条 SSE 到来之前的状态
    useResearchStore.setState({
      pendingCheckpoint: {
        id: "cp1",
        phase: "intent",
        status: "pending",
        options: {},
        prompt: "p1",
        userChoice: null,
        decidedAt: null,
      },
      resolutionInFlight: true,
    });

    // 模拟下一条 SSE checkpoint 事件到达
    const nextCheckpoint = {
      name: "research.checkpoint" as const,
      payload: { checkpointId: "cp2", phase: "planning", prompt: "p2", options: {} },
    };
    const updated = applyResearchEvent(useResearchStore.getState(), nextCheckpoint);
    useResearchStore.setState(updated);

    // 两项均被新事件替换
    const state = useResearchStore.getState();
    expect(state.pendingCheckpoint).not.toBeNull();
    expect(state.pendingCheckpoint!.id).toBe("cp2");
    expect(state.resolutionInFlight).toBe(false);
  });

  it("answer submit: SSE disconnect resets resolutionInFlight to false", async () => {
    useResearchStore.setState({
      pendingCheckpoint: {
        id: "cp1",
        phase: "intent",
        status: "pending",
        options: {},
        prompt: "p1",
        userChoice: null,
        decidedAt: null,
      },
      resolutionInFlight: true,
    });

    // connectStream 抛出非断流错误时，resolutionInFlight 应被重置
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    await useResearchStore.getState().connectStream("s1");

    expect(useResearchStore.getState().resolutionInFlight).toBe(false);
  });

  it("answer 成功：pendingCheckpoint 保留但 resolutionInFlight=true，等 SSE 替换", async () => {
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
    // B4: pendingCheckpoint 保留（不清空），由下一个 SSE 事件来切换
    expect(useResearchStore.getState().pendingCheckpoint).not.toBeNull();
    expect(useResearchStore.getState().resolutionInFlight).toBe(true);
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

  // --- Task 12b：SSE 断线重连回放（sessionStorage 游标 + 按 seq 去重） ---

  it("重连带上游标：上一轮最后的 seq 经 sessionStorage 回传 lastEventId", async () => {
    // 首次连接：流里带 id: 5 的事件 ⇒ store 记住游标。
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      body: sseStream('id: 5\nevent: research.intent\ndata: {"intent":"research"}\n\n'),
    });
    vi.stubGlobal("fetch", fetchMock);

    await useResearchStore.getState().connectStream("s1");

    expect(sessionStorage.getItem("research:lastEventId:s1")).toBe("5");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/research/stream?sessionId=s1");

    // 断线重连：URL 必须带上记住的游标（本任务唯一的用户可见行为）。
    fetchMock.mockClear();
    fetchMock.mockResolvedValue({ ok: true, body: sseStream() });
    await useResearchStore.getState().connectStream("s1");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/research/stream?sessionId=s1&lastEventId=5",
    );
  });

  it("游标按会话隔离：不同 sessionId 各记各的", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        body: sseStream('id: 9\nevent: research.intent\ndata: {"intent":"research"}\n\n'),
      }),
    );

    await useResearchStore.getState().connectStream("sA");

    expect(sessionStorage.getItem("research:lastEventId:sA")).toBe("9");
    expect(sessionStorage.getItem("research:lastEventId:sB")).toBeNull();
  });

  it("sessionStorage 不可用（getItem/setItem 抛错）：流仍建立、事件仍分发、不抛", async () => {
    const blocked: Storage = {
      getItem: () => {
        throw new Error("sessionStorage blocked");
      },
      setItem: () => {
        throw new Error("sessionStorage blocked");
      },
      removeItem: () => {},
      clear: () => {},
      key: () => null,
      length: 0,
    };
    const original = Object.getOwnPropertyDescriptor(window, "sessionStorage");
    Object.defineProperty(window, "sessionStorage", { configurable: true, value: blocked });
    try {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        body: sseStream('id: 3\nevent: research.intent\ndata: {"intent":"research"}\n\n'),
      });
      vi.stubGlobal("fetch", fetchMock);

      await expect(useResearchStore.getState().connectStream("s1")).resolves.toBeUndefined();

      // 读失败当无游标：URL 不带 lastEventId（不能因兜底失败而炸掉整条流）。
      expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/research/stream?sessionId=s1");
      // 写失败不影响投递：事件照常落地。
      expect(useResearchStore.getState().events.map((e) => e.name)).toEqual([
        "research.intent",
      ]);
    } finally {
      if (original) Object.defineProperty(window, "sessionStorage", original);
    }
  });

  it("按 seq 去重：订阅/回放窗口重叠的同一事件只投递一次", async () => {
    // 后端先订阅再查 outbox ⇒ 落在窗口内的事件既进订阅队列又在回放结果里，投递两次。
    const stream = sseStream(
      'id: 7\nevent: research.intent\ndata: {"intent":"research"}\n\n',
      'id: 7\nevent: research.intent\ndata: {"intent":"research"}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    expect(useResearchStore.getState().events).toHaveLength(1);
  });

  it("去重是单调游标：乱序/更旧的 seq 也被丢弃", async () => {
    const stream = sseStream(
      'id: 5\nevent: research.intent\ndata: {"intent":"research"}\n\n',
      'id: 8\nevent: research.esl\ndata: {"metrics":[]}\n\n',
      'id: 6\nevent: research.intent\ndata: {"intent":"research"}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    expect(useResearchStore.getState().events).toHaveLength(2);
  });

  it("无 id 的重复帧不去重（无法判定 seq，两条都透传）", async () => {
    const stream = sseStream(
      'event: research.intent\ndata: {"intent":"research"}\n\n',
      'event: research.intent\ndata: {"intent":"research"}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    await useResearchStore.getState().connectStream("s1");

    expect(useResearchStore.getState().events).toHaveLength(2);
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

  // §3.2: 预算闸门降级可见化 —— degradedNotice 是 degraded 类**唯一**的 UI 面。
  it("预算计量降级事件 ⇒ 置 degradedNotice 且不关流", () => {
    const initial = buildInitialState();
    const event = {
      name: "research.error" as const,
      payload: {
        code: "budget_metering_degraded",
        message: "会话成本计量连续读取失败：本轮预算闸门失效并放行",
        uiHint: "degraded" as const,
      },
    };
    const next = applyResearchEvent(initial, event);

    expect(next.degradedNotice).toBe("会话成本计量连续读取失败：本轮预算闸门失效并放行");
    // 流必须保持打开：闸门放行后状态机继续推进，后面还有 done（terminal 才关流）
    expect(next.streaming).toBeUndefined();
    expect(next.error).toBeUndefined();
    expect(next.conflictError).toBeUndefined();
  });

  it("degraded 无 message ⇒ 回退**已翻译**文案（不写空串，也不写裸 i18n key）", () => {
    const next = applyResearchEvent(buildInitialState(), {
      name: "research.error" as const,
      payload: { code: "budget_metering_degraded", uiHint: "degraded" as const },
    });

    // 页面 Alert 直渲 message（不过 t()）⇒ 落 key 会让用户看到字面 "research.progress.degraded"
    expect(next.degradedNotice).toBe(i18n.t("research.progress.degraded"));
    expect(next.degradedNotice).not.toBe("research.progress.degraded");
    expect(next.degradedNotice).toBeTruthy();
  });

  it("整类 degraded 都置 degradedNotice（不只 budget_metering_degraded）", () => {
    // 分支按 uiHint 的**类**（规则 ③）：既有 degraded code（逐步失败/LLM 不可用/校验失败/
    // 假设生成失败）此前对用户完全静默，本次统一补上这个面 —— 用 step_failed 钉住。
    const next = applyResearchEvent(buildInitialState(), {
      name: "research.error" as const,
      payload: { code: "step_failed", message: "第 2 步执行失败", uiHint: "degraded" as const },
    });

    expect(next.degradedNotice).toBe("第 2 步执行失败");
    expect(next.streaming).toBeUndefined(); // 仍不关流
  });

  it("terminal/conflict 不动 degradedNotice（各自已有自己的面）", () => {
    const initial = buildInitialState();
    const terminal = applyResearchEvent(initial, {
      name: "research.error" as const,
      payload: { code: "turn_failed", message: "本轮失败", uiHint: "terminal" as const },
    });
    expect(terminal.degradedNotice).toBeUndefined();

    const conflict = applyResearchEvent(initial, {
      name: "research.error" as const,
      payload: { code: "checkpoint_conflict", message: "冲突", uiHint: "conflict" as const },
    });
    expect(conflict.degradedNotice).toBeUndefined();
  });

  it("uiHint 缺失/未知 ⇒ 也不置 degradedNotice（认不出就不提示，fail-safe）", () => {
    const next = applyResearchEvent(buildInitialState(), {
      name: "research.error" as const,
      payload: { code: "mystery", message: "未知" },
    });

    expect(next.degradedNotice).toBeUndefined();
  });

  it("buildInitialState 带 degradedNotice: null（新会话无降级提示）", () => {
    expect(buildInitialState().degradedNotice).toBeNull();
  });

  it("reset 清 degradedNotice", () => {
    useResearchStore.setState({ degradedNotice: "上一轮的降级提示" });

    useResearchStore.getState().reset();

    expect(useResearchStore.getState().degradedNotice).toBeNull();
  });

  it("开新流即清上一轮的 degradedNotice（与 error 同生命周期）", async () => {
    stubAbortableStream();
    useResearchStore.setState({ degradedNotice: "上一轮的降级提示" });

    // `set({ streaming: true, … })` 在首个 await 之前同步执行 ⇒ 可直接断言
    const pending = useResearchStore.getState().connectStream("s1");
    expect(useResearchStore.getState().degradedNotice).toBeNull();

    useResearchStore.getState().reset(); // 收尾：断流让 pending 落地
    await pending;
  });

  // 跨会话泄漏：切到**已结束**会话不建流（useResearchSession 对终态会话直接 return），
  // 故 connectStream 那条清除点够不着 —— openSession / sendQuestion 必须自己清。
  it("openSession 清上一会话的 degradedNotice（切到已结束会话也不残留）", async () => {
    const detail = {
      session: makeSession({ status: "done" }),
      turns: [],
      pendingCheckpoint: null,
    };
    httpMock.get.mockResolvedValue({ data: detail });
    useResearchStore.setState({ degradedNotice: "上一个会话的降级提示" });

    await useResearchStore.getState().openSession("s2");

    expect(useResearchStore.getState().degradedNotice).toBeNull();
  });

  it("sendQuestion 清上一会话的 degradedNotice", async () => {
    httpMock.post.mockResolvedValue({ data: makeSession() });
    useResearchStore.setState({ degradedNotice: "上一个会话的降级提示" });

    await useResearchStore.getState().sendQuestion("新问题");

    expect(useResearchStore.getState().degradedNotice).toBeNull();
  });
});
