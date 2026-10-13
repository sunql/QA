// SSE 帧解析器：状态化切帧，跨块保留未完成 \r（避免 consumeFrames 归一化误切）。
// 完整 \n\n 才切帧；行内 \r 原样保留；EOF 守门用 flush。

export interface StreamFrame {
  event: string;
  data: string;
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
    for (const line of raw.split("\n")) {
      if (line.startsWith("event: ")) event = line.slice(7).trim();
      else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
    }
    if (!event && dataLines.length === 0) return null;
    return { event, data: dataLines.join("\n") };
  }
}
