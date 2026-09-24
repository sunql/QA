/** Wiki Chat API 客户端（feat-wiki-chat）。
 *
 * POST /api/v1/wiki/chat — SSE 流式问答（语义检索上下文 + LLM 合成）。
 * SSE 解析照 api/document.ts 的 doc_qa 模式：axios 不支持流式，走原生
 * fetch + ReadableStream，按空行拆帧，取 event:/data: 行分发。
 */

import type { WikiChatEventKind, WikiChatRequestPayload, WikiChatSseEvent } from "../types/wikiChat";

const API_ENDPOINT = "/api/v1/wiki/chat";

function evTypeToKind(t: string | undefined): WikiChatEventKind {
  switch (t) {
    case "qa_meta": return "meta";
    case "qa_citations": return "citations";
    case "token": return "token";
    case "qa_done": return "done";
    case "error": return "error";
    default: return "error";
  }
}

/**
 * 发送一轮 Wiki Chat 问答并逐事件回调。
 * @param payload 会话 id + 问题（topK/dimension/modelId 可选）
 * @param onEvent 每个 SSE 事件的回调（token 事件逐块到达）
 * @param signal 可选中止信号（组件卸载时中断流）
 */
export async function sendWikiChat(
  payload: WikiChatRequestPayload,
  onEvent: (event: WikiChatSseEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(API_ENDPOINT, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // 按 \n\n 拆 SSE 帧
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      const dataLine = part.split("\n").find((l) => l.startsWith("data: "));
      if (!dataLine) continue;
      try {
        const obj = JSON.parse(dataLine.slice("data: ".length));
        const evType = part
          .split("\n")
          .find((l) => l.startsWith("event: "))
          ?.slice("event: ".length)
          .trim();
        onEvent({ ...obj, kind: evTypeToKind(evType) });
      } catch {
        // 跳过解析失败的帧
      }
    }
  }
}
