import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { sendMessage, sendMessageStream, resumeMultiStepRun, type StepResultView } from "../api/chat";
import { useAuthStore } from "../stores/authStore";
import { DEFAULT_TENANT_ID } from "../config";
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
    useAuthStore.setState({ token: null });
  });

  it("sendMessageStream 登录态注入 SSOT auth headers（Authorization + X-Tenant-Id）", async () => {
    useAuthStore.setState({ token: "test-jwt" });
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body: sseStream() });
    vi.stubGlobal("fetch", fetchMock);

    await sendMessageStream(makePayload(), {});

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers).toMatchObject({
      Authorization: "Bearer test-jwt",
      "X-Tenant-Id": DEFAULT_TENANT_ID,
    });
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

  // 非流式响应也走同一道白名单：只有 SSE 帧做校验会让同一份后端数据在两条路径
  // 下表现不同（流式吞掉未知类型、非流式原样透传给渲染器）。
  it("sendMessage 收窄未知 chartType 与非对象 chartOption（与流式同一口径）", async () => {
    httpMock.post.mockResolvedValue({
      data: {
        answer: "查询完成",
        intent: "query",
        chartType: "radar",
        chartOption: "not-an-option",
        data: [{ NAME: "A" }],
        tokensUsed: 45,
        cost: 0.00006,
      } as unknown as ChatResponse,
    });

    const result = await sendMessage(makePayload());
    expect(result.chartType).toBeNull();
    expect(result.chartOption).toBeNull();
  });

  it("sendMessage 收窄 steps 里每步的图表字段", async () => {
    httpMock.post.mockResolvedValue({
      data: {
        answer: "查询完成",
        intent: "multi_step",
        chartType: null,
        chartOption: null,
        steps: [
          {
            stepIndex: 0,
            description: "各供应商收货量",
            subQuestion: "各供应商收货量",
            sql: "SELECT 1",
            summary: "9812",
            error: null,
            chartType: "hbar",
            chartOption: { series: [{ type: "bar" }] },
          },
          {
            stepIndex: 1,
            description: "按月的收货量",
            subQuestion: "按月的收货量",
            sql: "SELECT 2",
            summary: "12",
            error: null,
            chartType: "bogus",
            chartOption: [],
          },
        ],
        tokensUsed: 45,
        cost: 0.00006,
      } as unknown as ChatResponse,
    });

    const result = await sendMessage(makePayload());
    expect(result.steps?.[0].chartType).toBe("hbar");
    expect(result.steps?.[0].chartOption).toEqual({ series: [{ type: "bar" }] });
    expect(result.steps?.[1].chartType).toBeNull();
    expect(result.steps?.[1].chartOption).toBeNull();
  });

  it("sendMessage 响应不带 steps 时保持 undefined（不伪造空数组）", async () => {
    httpMock.post.mockResolvedValue({
      data: {
        answer: "查询完成",
        intent: "query",
        chartType: null,
        chartOption: null,
        tokensUsed: 45,
        cost: 0.00006,
      } as unknown as ChatResponse,
    });

    const result = await sendMessage(makePayload());
    expect(result.steps).toBeUndefined();
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
    expect(onChart).toHaveBeenCalledWith({
      chartType: null,
      chartOption: {},
      tableOption: null,
      visualRationale: null,
      data: [],
    });
  });

  // 决策引擎新增 6 类（hbar/donut/heatmap/kpi/combo/waterfall）。白名单漏同步的
  // 表现是「图不见了但没有任何报错」——后端发了、前端静默置 null，所以逐个钉住。
  it.each(["hbar", "donut", "heatmap", "kpi", "combo", "waterfall"])(
    "chart 事件接受决策引擎新增的 chartType：%s",
    async (chartType) => {
      const stream = sseStream(
        `event: chart\ndata: {"chartType":"${chartType}","chartOption":{},"data":[]}\n\n`
      );
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

      const onChart = vi.fn();
      await sendMessageStream(makePayload(), { onChart });
      expect(onChart).toHaveBeenCalledWith({
        chartType,
        chartOption: {},
        tableOption: null,
        visualRationale: null,
        data: [],
      });
    }
  );

  it("step_result 事件透传每步自己的 chartType/chartOption（多步每步出图）", async () => {
    const stream = sseStream(
      'event: step_result\ndata: {"stepIndex":0,"description":"各供应商收货量","subQuestion":"各供应商收货量","sql":"SELECT 1","summary":"9812","error":null,"chartType":"hbar","chartOption":{"series":[{"type":"bar"}]}}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const results: StepResultView[] = [];
    await sendMessageStream(makePayload(), { onStepResult: (r) => results.push(r) });

    expect(results[0].chartType).toBe("hbar");
    expect(results[0].chartOption).toEqual({ series: [{ type: "bar" }] });
  });

  it("step_result 的非法 chartType 同样回退为 null（系统边界校验）", async () => {
    const stream = sseStream(
      'event: step_result\ndata: {"stepIndex":0,"description":"x","subQuestion":"x","sql":null,"summary":null,"error":"boom","chartType":"bogus"}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const results: StepResultView[] = [];
    await sendMessageStream(makePayload(), { onStepResult: (r) => results.push(r) });

    expect(results[0].chartType).toBeNull();
    expect(results[0].chartOption).toBeNull();
  });

  it("step_result 不带图表字段（失败步骤）时两字段为 null，不误报类型", async () => {
    const stream = sseStream(
      'event: step_result\ndata: {"stepIndex":1,"description":"x","subQuestion":"x","sql":null,"summary":null,"error":"boom"}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const results: StepResultView[] = [];
    await sendMessageStream(makePayload(), { onStepResult: (r) => results.push(r) });

    expect(results[0].chartType).toBeNull();
    expect(results[0].chartOption).toBeNull();
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

// ===== Task 7（可视化输出策略）：tableOption / visualRationale 收窄 =====
// 4 个 api 接入点（normalizeStepResult / normalizeChatResponse / chart 事件 / done 事件）
// 必须对同一份负载产出同一份收窄结果。共享 fixture + 顺序循环，避免复制断言（加第 5 处时漏掉）。
describe("api/chat 两个新字段收窄（0107）", () => {
  const TABLE_FIXTURE = { columns: ["NAME", "QTY"], rows: [{ NAME: "A", QTY: 1 }], truncated: true };
  const RATIONALE_FIXTURE = { code: "R04_TOPN_HBAR", params: { rows: 5 } };

  it("4 个接入点对同一份负载产出相同收窄结果", async () => {
    const sites: Array<{
      name: string;
      run: () => Promise<{ tableOption?: unknown; visualRationale?: unknown }>;
    }> = [
      {
        name: "step_result 事件（normalizeStepResult）",
        run: async () => {
          const stream = sseStream(
            `event: step_result\ndata: ${JSON.stringify({
              stepIndex: 0,
              description: "x",
              subQuestion: "x",
              tableOption: TABLE_FIXTURE,
              visualRationale: RATIONALE_FIXTURE,
            })}\n\n`
          );
          vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));
          const results: unknown[] = [];
          await sendMessageStream(makePayload(), { onStepResult: (r) => results.push(r) });
          return results[0] as { tableOption?: unknown; visualRationale?: unknown };
        },
      },
      {
        name: "非流式响应（normalizeChatResponse）",
        run: async () => {
          httpMock.post.mockResolvedValue({
            data: {
              answer: "查询完成",
              intent: "query",
              tableOption: TABLE_FIXTURE,
              visualRationale: RATIONALE_FIXTURE,
              tokensUsed: 0,
              cost: 0,
            } as unknown as ChatResponse,
          });
          const result = await sendMessage(makePayload());
          return { tableOption: result.tableOption, visualRationale: result.visualRationale };
        },
      },
      {
        name: "chart 事件",
        run: async () => {
          const stream = sseStream(
            `event: chart\ndata: ${JSON.stringify({
              chartType: "bar",
              chartOption: {},
              tableOption: TABLE_FIXTURE,
              visualRationale: RATIONALE_FIXTURE,
              data: [],
            })}\n\n`
          );
          vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));
          const charts: unknown[] = [];
          await sendMessageStream(makePayload(), { onChart: (c) => charts.push(c) });
          return charts[0] as { tableOption?: unknown; visualRationale?: unknown };
        },
      },
      {
        name: "done 事件（仅 visualRationale）",
        run: async () => {
          const stream = sseStream(
            `event: done\ndata: ${JSON.stringify({
              tokensUsed: 0,
              cost: 0,
              visualRationale: RATIONALE_FIXTURE,
            })}\n\n`
          );
          vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));
          const dones: unknown[] = [];
          await sendMessageStream(makePayload(), { onDone: (s) => dones.push(s) });
          return dones[0] as { visualRationale?: unknown };
        },
      },
    ];

    const results: Array<{ tableOption?: unknown; visualRationale?: unknown }> = [];
    for (const site of sites) {
      results.push(await site.run());
    }

    for (const [i, site] of sites.entries()) {
      if (site.name.startsWith("done")) {
        // done 帧不带 tableOption，只断言 rationale
        expect(results[i].visualRationale).toEqual(RATIONALE_FIXTURE);
      } else {
        expect(results[i]).toMatchObject({
          tableOption: TABLE_FIXTURE,
          visualRationale: RATIONALE_FIXTURE,
        });
      }
    }
  });

  it("非法 tableOption（rows 非数组）在 chart 事件收窄为 null", async () => {
    const stream = sseStream(
      'event: chart\ndata: {"chartType":"bar","chartOption":{},"tableOption":{"columns":["NAME"],"rows":"oops"},"visualRationale":{"code":"R04_TOPN_HBAR","params":{"rows":5}},"data":[]}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const charts: unknown[] = [];
    await sendMessageStream(makePayload(), { onChart: (c) => charts.push(c) });

    expect((charts[0] as { tableOption: unknown }).tableOption).toBeNull();
    expect((charts[0] as { visualRationale: unknown }).visualRationale).toEqual({
      code: "R04_TOPN_HBAR",
      params: { rows: 5 },
    });
  });
});

// ===== Task 9（多步持久化）：runId 透传 / 压缩事件 / 续跑端点 =====
describe("api/chat 续跑与压缩（Task 9）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    useAuthStore.setState({ token: null });
  });

  it("multi_step_plan 携带 runId 时作为第二个参数交给回调", async () => {
    const stream = sseStream(
      'event: multi_step_plan\ndata: {"runId":"r-9","steps":[{"stepIndex":0,"description":"d","subQuestion":"q","aggregationOnly":false}]}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const seen: Array<[unknown, unknown]> = [];
    await sendMessageStream(makePayload(), {
      onStepPlanOverview: (steps, runId) => seen.push([steps, runId]),
    });

    expect(seen).toHaveLength(1);
    expect(seen[0]?.[0]).toHaveLength(1);
    expect(seen[0]?.[1]).toBe("r-9");
  });

  it("multi_step_plan 不带 runId（单步路径）时第二个参数为 undefined", async () => {
    const stream = sseStream(
      'event: multi_step_plan\ndata: {"steps":[{"stepIndex":0,"description":"d","subQuestion":"q","aggregationOnly":false}]}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const seen: unknown[] = [];
    await sendMessageStream(makePayload(), {
      onStepPlanOverview: (_steps, runId) => seen.push(runId),
    });

    expect(seen).toEqual([undefined]);
  });

  it("step_compressed 事件分发给 onStepCompressed", async () => {
    const stream = sseStream(
      'event: step_compressed\ndata: {"stepIndex":0,"originalRows":1000,"compressedRows":30}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const seen: unknown[] = [];
    await sendMessageStream(makePayload(), {
      onStepCompressed: (payload) => seen.push(payload),
    });

    expect(seen).toEqual([{ stepIndex: 0, originalRows: 1000, compressedRows: 30 }]);
  });

  it("step_compressed 非法负载（缺 stepIndex）不触发回调", async () => {
    const stream = sseStream(
      'event: step_compressed\ndata: {"originalRows":1000,"compressedRows":30}\n\n'
    );
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: stream }));

    const seen: unknown[] = [];
    await sendMessageStream(makePayload(), {
      onStepCompressed: (payload) => seen.push(payload),
    });

    expect(seen).toHaveLength(0);
  });

  it("resumeMultiStepRun POST 到 resume 端点，带 Idempotency-Key 与 camelCase body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: sseStream() }));

    await resumeMultiStepRun("r-1", 2, {});

    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(String(url)).toContain("/chat/multi-step/r-1/resume");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ fromStepIndex: 2 });
    // 幂等键由前端生成，后端据此去重（spec §7.3）
    expect(typeof (init?.headers as Record<string, string>)["Idempotency-Key"]).toBe("string");
  });

  it("resumeMultiStepRun 对 runId 做 URL 编码（防路径注入）", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body: sseStream() }));

    await resumeMultiStepRun("a/b c", 0, {});

    const [url] = vi.mocked(fetch).mock.calls[0];
    expect(String(url)).toContain("/chat/multi-step/a%2Fb%20c/resume");
  });
});
