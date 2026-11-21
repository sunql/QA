/**
 * SSE 解析与流式读取（Copilot 入口用）。
 *
 * 为什么自己解析而不引 EventSource：
 * - `EventSource` **不支持 POST**，也不带自定义头（我们要 Authorization）；
 * - 后端帧格式就是 `event: <name>\ndata: <json>\n\n`（`StreamEvent.toSse`）。
 *
 * 两条纪律：
 * 1. **跨 chunk 拼接**：一个事件可能被网络拆成两段 ⇒ 只解析完整的 `\n\n` 块；
 *    剩下的留在 buffer 里等下一块（丢弃半个事件 = 静默丢步骤）。
 * 2. **坏帧不炸整条流**：单帧 JSON 解析失败时跳过并回调 `onError`，后续事件继续。
 */

export interface SseEvent {
  event: string;
  data: unknown;
}

/** 从文本块里切出**完整**的 SSE 帧；返回事件与剩余缓冲。 */
export function parseSseChunk(buffer: string): {
  events: SseEvent[];
  rest: string;
} {
  const events: SseEvent[] = [];
  let rest = buffer;
  let index = rest.indexOf("\n\n");
  while (index !== -1) {
    const frame = rest.slice(0, index);
    rest = rest.slice(index + 2);
    const parsed = parseFrame(frame);
    if (parsed) events.push(parsed);
    index = rest.indexOf("\n\n");
  }
  return { events, rest };
}

function parseFrame(frame: string): SseEvent | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const rawLine of frame.split("\n")) {
    const line = rawLine.trimEnd();
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  const raw = dataLines.join("\n");
  try {
    return { event, data: JSON.parse(raw) };
  } catch {
    return { event, data: { raw } };
  }
}

/** 把一个 `text/event-stream` 响应读成事件序列。 */
export async function readSseStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (event: SseEvent) => void,
  options: { onParseError?: (raw: string) => void } = {},
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { events, rest } = parseSseChunk(buffer);
      buffer = rest;
      for (const event of events) {
        if ("raw" in (event.data as Record<string, unknown>)) {
          options.onParseError?.(String((event.data as { raw: string }).raw));
          continue;
        }
        onEvent(event);
      }
    }
  } finally {
    reader.releaseLock();
  }
}
