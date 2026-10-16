// SSE 帧解析器：状态化切帧，跨块保留未完成 \r（避免 consumeFrames 归一化误切）。
// 完整 \n\n 才切帧；行内 \r 原样保留；EOF 守门用 flush。
//
// 解析 SSE 规范的三类字段：
//   - `event: <name>`（行首是 "event: "） — 帧名
//   - `data: <chunk>`（行首是 "data: "） — 帧数据（多行按 \n 拼）
//   - `id: <seq>`   （行首是 "id: " 或 "id:"） — 帧序号；非数字 / 空值
//                   ⇒ 帧 id 为 undefined（SSE 规范：同帧多个 id: 行时
//                   **最后一个**生效；空 / 非整数值重置而非保留更早值）
// 注释 / 心跳行（行首是 ":"）按 SSE 规范跳过。

export interface StreamFrame {
  event: string;
  data: string;
  /** SSE 规范字段。空 / 非整数 ⇒ undefined。 */
  id?: number;
}

export class StreamChunkParser {
  private buffer = "";

  feed(chunk: string): StreamFrame[] {
    this.buffer += chunk;
    return this._extractFrames();
  }

  flush(): StreamFrame[] {
    if (!this.buffer) return [];
    // EOF 守门：强制把残留 buffer 当 1 帧返回（哪怕不完整，缺少 \n\n）。
    // 不走 _extractFrames（它只在 buffer 含完整 \n\n 时才切帧）。
    const frames: StreamFrame[] = [];
    const parsed = this._parseFrame(this.buffer);
    if (parsed !== null) frames.push(parsed);
    this.buffer = "";
    return frames;
  }

  private _extractFrames(): StreamFrame[] {
    const frames: StreamFrame[] = [];
    let boundary: number;
    // 支持 \n\n 和 \r\n\r\n 两种帧分隔符。
    // 优先匹配 CRLF（4 字符更长，不可能与 LF 混淆）。
    while ((boundary = this.buffer.indexOf("\r\n\r\n")) !== -1) {
      const raw = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 4);
      const parsed = this._parseFrame(raw);
      if (parsed !== null) frames.push(parsed);
    }
    // 回退 LF-only（纯 LF 环境或单次完整帧）。
    while ((boundary = this.buffer.indexOf("\n\n")) !== -1) {
      const raw = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const parsed = this._parseFrame(raw);
      if (parsed !== null) frames.push(parsed);
    }
    return frames;
  }

  private _parseFrame(raw: string): StreamFrame | null {
    let event = "";
    const dataLines: string[] = [];
    // SSE 规范：同帧出现多个 `id:` 时**最后一个**生效。空值 / 非整数
    // ⇒ 该帧 id 为 undefined（**重置**更早的有效值，不是 continue 跳过）。
    let lastIdRaw: string | undefined;
    let sawIdLine = false;
    for (const line of raw.split("\n")) {
      // 行首是 ":" 的注释 / 心跳行：SSE 规范要求跳过
      if (line.startsWith(":")) continue;
      if (line.startsWith("event: ")) {
        const val = line.slice(7);
        // 仅从 event 行值末尾去掉 \r（CRLF 分割残余），不碰 data 内容。
        event = (val.endsWith("\r") ? val.slice(0, -1) : val).trim();
      } else if (line.startsWith("data: ")) {
        dataLines.push(line.slice(6));
      } else if (line.startsWith("id:")) {
        // 兼容 "id: x" 和 "id:x"（SSE 规范说至少一个空格；后端当前用前者）
        const after = line.startsWith("id: ") ? line.slice("id: ".length) : line.slice("id:".length);
        lastIdRaw = after.trimEnd();
        sawIdLine = true;
      } else {
        // SSE 规范：没有字段前缀的行当作 ``data: <整行>`` 拼接。
        // 这条处理跨 chunk 边界 —— 比如 chunk1 末尾停在 ``data: {"x": 1\r``，
        // chunk2 开头是 ``}``（``data: `` 前缀丢失），SSE 仍要求把它当 data
        // 续行，旧实现直接丢掉会让一帧的 JSON 残缺。
        dataLines.push(line);
      }
    }
    if (!event && dataLines.length === 0) return null;
    const frame: StreamFrame = { event, data: dataLines.join("\n") };
    if (sawIdLine) {
      const raw = lastIdRaw ?? "";
      const parsed = Number(raw);
      // 空 / 非整数 ⇒ undefined（**重置**先前有效值；与 SSE 规范
      // 「同帧最后一个 id: 生效」一致）
      frame.id = raw !== "" && Number.isInteger(parsed) ? parsed : undefined;
    }
    return frame;
  }
}
