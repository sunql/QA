import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { sendMessage, sendMessageStream } from "../api/chat";
import type { ChatRequest, ChatResponse } from "../types/chat";

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

function makePayload(): ChatRequest {
  return {
    sessionId: "s1",
    question: "各供应商的收货数量汇总",
    datasourceId: 1,
    history: [],
  };
}

describe("api/chat", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sendMessage 发送到 /chat 并返回响应", async () => {
    const payload: ChatRequest = makePayload();
    const resp: ChatResponse = {
      answer: "查询完成",
      intent: "query",
      sql: "SELECT 1 FROM DUAL",
      chartType: "pie",
      chartOption: { series: [] },
      data: [{ NAME: "A" }],
      tokensUsed: 45,
      cost: 0.00006,
    };
    httpMock.post.mockResolvedValue({ data: resp });

    const result = await sendMessage(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/chat", payload);
    expect(result.answer).toBe("查询完成");
    expect(result.chartType).toBe("pie");
  });

  it("sendMessageStream 按事件顺序分发 meta/sql/chart/token/done", async () => {
    const payload = makePayload();
    const frames = [
      'event: meta\ndata: {"intent":"query"}\n\n',
      'event: sql\ndata: {"sql":"SELECT 1 FROM DUAL"}\n\n',
      'event: chart\ndata: {"chartType":"pie","chartOption":{"series":[]},"data":[]}\n\n',
      'event: token\ndata: {"content":"查询完成，"}\n\n',
      'event: token\ndata: {"content":"共 2 条记录。"}\n\n',
      'event: done\ndata: {"tokensUsed":45,"cost":0.00006}\n\n',
    ];
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body: sseStream(...frames) });
    vi.stubGlobal("fetch", fetchMock);

    const received: string[] = [];
    await sendMessageStream(payload, {
      onMeta: (intent) => received.push(`meta:${intent}`),
      onSql: (sql) => received.push(`sql:${sql.length > 0}`),
      onChart: (chart) => received.push(`chart:${chart.chartType}`),
      onToken: (content) => received.push(`token:${content}`),
      onDone: ({ tokensUsed }) => received.push(`done:${tokensUsed}`),
    });

    expect(received).toEqual([
      "meta:query",
      "sql:true",
      "chart:pie",
      "token:查询完成，",
      "token:共 2 条记录。",
      "done:45",
    ]);
    // 请求发往 /chat/stream，负载原样传递
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/chat/stream");
    expect(JSON.parse(init.body)).toEqual(payload);
  });

  it("sendMessageStream error 事件调用 onError（含 detail）", async () => {
    const stream = sseStream(
      'event: meta\ndata: {"intent":"query"}\n\n',
      'event: error\ndata: {"error":"数据源 99999 不存在","detail":"类 PRECEIPTD 不存在；属性 收货数量 不存在"}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const onError = vi.fn();
    await sendMessageStream(makePayload(), { onError });
    expect(onError).toHaveBeenCalledWith(
      "数据源 99999 不存在",
      "类 PRECEIPTD 不存在；属性 收货数量 不存在"
    );
  });

  it("sendMessageStream error 事件无 detail 时第二参为 undefined", async () => {
    const stream = sseStream(
      'event: meta\ndata: {"intent":"query"}\n\n',
      'event: error\ndata: {"error":"数据源 99999 不存在"}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const onError = vi.fn();
    await sendMessageStream(makePayload(), { onError });
    expect(onError).toHaveBeenCalledWith("数据源 99999 不存在", undefined);
  });

  it("sendMessageStream HTTP 非 200 时抛出错误", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 429 }));
    await expect(sendMessageStream(makePayload(), {})).rejects.toThrow("HTTP 429");
  });

  it("sendMessageStream 兼容 CRLF 换行的帧分隔（HIGH#2）", async () => {
    const stream = sseStream(
      'event: meta\r\ndata: {"intent":"query"}\r\n\r\n',
      'event: token\r\ndata: {"content":"查询完成，"}\r\n\r\n',
      'event: done\r\ndata: {"tokensUsed":45,"cost":0.00006}\r\n\r\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: string[] = [];
    await sendMessageStream(makePayload(), {
      onMeta: (intent) => received.push(`meta:${intent}`),
      onToken: (content) => received.push(`token:${content}`),
      onDone: ({ tokensUsed }) => received.push(`done:${tokensUsed}`),
    });

    expect(received).toEqual(["meta:query", "token:查询完成，", "done:45"]);
  });

  it("chart 事件中的非法 chartType 回退为 null（MEDIUM#5）", async () => {
    const stream = sseStream(
      'event: chart\ndata: {"chartType":"unknown_chart","chartOption":{},"data":[]}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const onChart = vi.fn();
    await sendMessageStream(makePayload(), { onChart });
    expect(onChart).toHaveBeenCalledWith({ chartType: null, chartOption: {}, data: [] });
  });

  it("sendMessageStream 分发多步事件 multi_step_plan/step_plan/step_result", async () => {
    const stream = sseStream(
      'event: multi_step_plan\ndata: {"steps":[{"stepIndex":0,"description":"2024 销售额","subQuestion":"2024年销售额","aggregationOnly":false},{"stepIndex":1,"description":"2025 销售额","subQuestion":"2025年销售额","aggregationOnly":false},{"stepIndex":2,"description":"对比","subQuestion":"汇总","aggregationOnly":true}]}\n\n',
      'event: step_plan\ndata: {"stepIndex":0,"description":"2024 销售额","subQuestion":"2024年销售额"}\n\n',
      'event: step_result\ndata: {"stepIndex":0,"description":"2024 销售额","subQuestion":"2024年销售额","sql":"SELECT 1","summary":"1000","error":null}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const overviews: unknown[][] = [];
    const plans: unknown[] = [];
    const results: unknown[] = [];
    await sendMessageStream(makePayload(), {
      onStepPlanOverview: (steps) => overviews.push(steps),
      onStepPlan: (step) => plans.push(step),
      onStepResult: (result) => results.push(result),
    });

    expect(overviews).toHaveLength(1);
    expect(overviews[0]).toHaveLength(3);
    expect(overviews[0]?.[2]).toMatchObject({ stepIndex: 2, aggregationOnly: true });
    expect(plans).toEqual([{ stepIndex: 0, description: "2024 销售额", subQuestion: "2024年销售额" }]);
    expect(results).toHaveLength(1);
    expect(results[0]).toMatchObject({ stepIndex: 0, sql: "SELECT 1", summary: "1000" });
  });

  it("step_plan 非法负载（缺 stepIndex）被丢弃，不触发回调", async () => {
    const stream = sseStream(
      'event: step_plan\ndata: {"description":"缺少 stepIndex","subQuestion":"x"}\n\n',
      'event: step_result\ndata: {"stepIndex":0,"description":"d","subQuestion":"q"}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const onStepPlan = vi.fn();
    const onStepResult = vi.fn();
    await sendMessageStream(makePayload(), { onStepPlan, onStepResult });

    expect(onStepPlan).not.toHaveBeenCalled();
    expect(onStepResult).toHaveBeenCalledTimes(1);
  });

  it("sendMessageStream 分发 plan 事件（合法 query plan）", async () => {
    const stream = sseStream(
      'event: plan\ndata: {"plan":{"target":"Order","selectedClasses":["Order"],"selectedProperties":["amount"],"conditions":[],"aggregations":[],"groupBy":[],"joins":[],"sortBy":[],"rowLimit":100}}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: unknown[] = [];
    await sendMessageStream(makePayload(), {
      onPlan: (plan) => received.push(plan),
    });

    expect(received).toHaveLength(1);
    expect(received[0]).toMatchObject({ target: "Order" });
  });

  it("sendMessageStream JSON 解析失败时静默丢弃（catch 分支）", async () => {
    const stream = sseStream('event: meta\ndata: {invalid json}\n\n');
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    // 不应抛错
    await expect(sendMessageStream(makePayload(), {})).resolves.toBeUndefined();
  });

  it("sendMessageStream 分发 data_quality 事件（合法 badges）", async () => {
    const stream = sseStream(
      'event: data_quality\ndata: {"badges":[{"targetTable":"t_order","evaluated":true,"overallScore":"0.95","evaluatedAt":"2026-09-01T00:00:00Z","rulesCount":5},{"targetTable":"bad","evaluated":false,"overallScore":null,"evaluatedAt":null,"rulesCount":null}]}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: { badges: unknown[] }[] = [];
    await sendMessageStream(makePayload(), {
      onDataQuality: (payload) => received.push(payload),
    });

    expect(received).toHaveLength(1);
    expect(received[0].badges).toHaveLength(2);
    expect(received[0].badges[0]).toMatchObject({ targetTable: "t_order" });
  });

  it("data_quality 中非法 badge 被过滤掉（缺字段）", async () => {
    const stream = sseStream(
      'event: data_quality\ndata: {"badges":[{"targetTable":"t_order","evaluated":true,"overallScore":"0.95","evaluatedAt":"2026-09-01T00:00:00Z","rulesCount":5},{"invalid":"shape"}]}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: { badges: unknown[] }[] = [];
    await sendMessageStream(makePayload(), {
      onDataQuality: (payload) => received.push(payload),
    });

    // 只有合法 badge 保留
    expect(received).toHaveLength(1);
    expect(received[0].badges).toHaveLength(1);
  });

  it("data_quality 中 badges 非数组 → 不触发回调", async () => {
    const stream = sseStream(
      'event: data_quality\ndata: {"badges":"not an array"}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: { badges: unknown[] }[] = [];
    await sendMessageStream(makePayload(), {
      onDataQuality: (payload) => received.push(payload),
    });

    expect(received).toHaveLength(0);
  });

  it("跨分块截断的多字节 UTF-8 字符仍完整送达", async () => {
    // "全" = E5 85 A8：把 3 字节拆到两个分块，验证流式解码跨块重组不丢字
    const encoder = new TextEncoder();
    const part1 = encoder.encode('event: token\ndata: {"content":"完');
    const part2 = encoder.encode('全"}\n\n');
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([...part1, part2[0]]));
        controller.enqueue(part2.slice(1));
        controller.close();
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const contents: string[] = [];
    await sendMessageStream(makePayload(), { onToken: (content) => contents.push(content) });
    expect(contents).toEqual(["完全"]);
  });
});

describe("sendMessageStream class_recall 事件", () => {
  it("分发 class_recall 事件（合法诊断对象）", async () => {
    const stream = sseStream(
      'event: class_recall\ndata: {"mode":"expanded","hitCount":3,"classCount":12,"truncated":true}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: unknown[] = [];
    await sendMessageStream(makePayload(), {
      onClassRecall: (info) => received.push(info),
    });

    expect(received).toHaveLength(1);
    expect(received[0]).toEqual({
      mode: "expanded",
      hitCount: 3,
      classCount: 12,
      truncated: true,
    });
  });

  it("class_recall 非法负载（缺 mode）不触发回调", async () => {
    const stream = sseStream(
      'event: class_recall\ndata: {"hitCount":3}\n\n',
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const received: unknown[] = [];
    await sendMessageStream(makePayload(), {
      onClassRecall: (info) => received.push(info),
    });

    expect(received).toHaveLength(0);
  });
});
