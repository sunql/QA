import { describe, expect, it } from "vitest";
import { parseSseChunk } from "./sseStream";

describe("parseSseChunk", () => {
  it("解析单个完整帧", () => {
    const { events, rest } = parseSseChunk(
      'event: copilot_run\ndata: {"runId":"r1"}\n\n',
    );
    expect(rest).toBe("");
    expect(events).toEqual([{ event: "copilot_run", data: { runId: "r1" } }]);
  });

  it("解析多个帧", () => {
    const { events } = parseSseChunk(
      'event: a\ndata: {"n":1}\n\nevent: b\ndata: {"n":2}\n\n',
    );
    expect(events.map((e) => e.event)).toEqual(["a", "b"]);
  });

  it("半帧留在缓冲里（不丢步骤）", () => {
    const { events, rest } = parseSseChunk(
      'event: a\ndata: {"n":1}\n\nevent: b\ndata: {"n"',
    );
    expect(events).toHaveLength(1);
    expect(rest).toBe('event: b\ndata: {"n"');
  });

  it("跨 chunk 拼接后能解析完", () => {
    const first = parseSseChunk('event: a\ndata: {"n"');
    const second = parseSseChunk(`${first.rest}:1}\n\n`);
    expect(second.events).toEqual([{ event: "a", data: { n: 1 } }]);
  });

  it("中文与换行转义正常", () => {
    const { events } = parseSseChunk(
      'event: done\ndata: {"answer":"第一行\\n第二行 中文"}\n\n',
    );
    expect((events[0].data as { answer: string }).answer).toContain("中文");
  });

  it("坏 JSON 帧不吞掉后面的帧", () => {
    const { events } = parseSseChunk(
      'event: bad\ndata: {not json}\n\nevent: ok\ndata: {"n":1}\n\n',
    );
    expect(events).toHaveLength(2);
    expect(events[0]).toEqual({ event: "bad", data: { raw: "{not json}" } });
    expect(events[1]).toEqual({ event: "ok", data: { n: 1 } });
  });

  it("无 data 的帧被忽略", () => {
    const { events } = parseSseChunk("event: ping\n\n");
    expect(events).toEqual([]);
  });
});
