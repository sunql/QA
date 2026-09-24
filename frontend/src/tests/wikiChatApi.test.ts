// Wiki Chat API SSE 解析测试（feat-wiki-chat）。
// mock fetch ReadableStream，逐事件断言分发（照 doc_qa SSE 客户端契约）。

import { describe, it, expect, vi, afterEach } from "vitest";
import { sendWikiChat } from "../api/wikiChat";
import type { WikiChatSseEvent } from "../types/wikiChat";

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

describe("sendWikiChat SSE parsing", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("parses qa_meta / qa_citations / token / qa_done frames in order", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
      'event: qa_meta\ndata: {"intent":"wiki_qa"}\n\n',
      'event: qa_citations\ndata: {"citations":[{"id":1,"pageId":"PAGE-A","title":"准入规则","score":0.55}]}\n\n',
      "event: token\ndata: {\"content\":\"答\"}\n\n",
      'event: qa_done\ndata: {"tokensUsed":15,"cost":0.001,"modelName":"m1"}\n\n',
    ])));

    const events: WikiChatSseEvent[] = [];
    await sendWikiChat({ sessionId: "s1", question: "门槛" }, (e) => events.push(e));

    expect(events.map((e) => e.kind)).toEqual(["meta", "citations", "token", "done"]);
    expect(events[1].citations?.[0]).toMatchObject({ id: 1, pageId: "PAGE-A", score: 0.55 });
    expect(events[2].content).toBe("答");
    expect(events[3]).toMatchObject({ tokensUsed: 15, modelName: "m1" });

    // 请求契约：POST /api/v1/wiki/chat + JSON body camelCase
    const fetchMock = vi.mocked(fetch);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/wiki/chat",
      expect.objectContaining({ method: "POST" }),
    );
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect(body).toMatchObject({ sessionId: "s1", question: "门槛" });
  });

  it("maps error event and skips malformed frames", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sseResponse([
      "not-an-sse-frame\n",
      'event: error\ndata: {"error":"boom","errorType":"LLM"}\n\n',
    ])));

    const events: WikiChatSseEvent[] = [];
    await sendWikiChat({ sessionId: "s1", question: "q" }, (e) => events.push(e));

    expect(events).toHaveLength(1);
    expect(events[0].kind).toBe("error");
    expect(events[0].error).toBe("boom");
  });

  it("throws on non-2xx response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response("err", { status: 503 }),
    ));
    await expect(
      sendWikiChat({ sessionId: "s1", question: "q" }, () => undefined),
    ).rejects.toThrow("HTTP 503");
  });
});
