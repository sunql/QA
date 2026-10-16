import { describe, it, expect } from "vitest";
import { StreamChunkParser } from "../stores/stream_chunk_parser";

describe("streamChunkParser CRLF 跨块保留", () => {
  it("'\\r\\n' 不归一化为 '\\n\\n'（fix consumeFrames bug）", () => {
    const parser = new StreamChunkParser();
    const f1 = parser.feed("event: meta\r");
    expect(f1).toEqual([]); // 边界不切
    const f2 = parser.feed("\ndata: {}\n\n"); // 跨块才完整 \n\n
    expect(f2).toEqual([{ event: "meta", data: "{}" }]);
  });

  it("chunk 内部完整 '\\n\\n' 立即切", () => {
    const parser = new StreamChunkParser();
    const frames = parser.feed("event: meta\ndata: {}\n\nevent: token\ndata: hi\n\n");
    expect(frames).toHaveLength(2);
    expect(frames[1]).toEqual({ event: "token", data: "hi" });
  });

  it("flush 强制输出残留（EOF 守门用）", () => {
    const parser = new StreamChunkParser();
    parser.feed("event: meta\ndata: {}");
    expect(parser.flush()).toEqual([{ event: "meta", data: "{}" }]);
  });

  it("行内 \\r 保留原样（不归一化到 \\n）", () => {
    const parser = new StreamChunkParser();
    // 第二行 data: 末尾的 \r 是 JSON 内容的一部分，不是行分隔符。
    const frames = parser.feed("event: meta\ndata: {\"x\": 1\r\n}\n\n");
    expect(frames).toHaveLength(1);
    // 关键：data 里的 \r 不被抹掉
    expect(frames[0].data).toContain("\r");
  });
});
