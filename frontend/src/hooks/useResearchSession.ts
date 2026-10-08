// 研究会话页的流编排 hook（feat-research-entry 修复轮）。
//
// 串行编排 openSession → connectStream（等 onOpen）→ submitTurn，消除原实现的
// 两个缺陷：①「先建流后提交」靠 void 三连发、无 await，submitTurn 可能抢在 SSE
// 订阅建立前跑（后端不重放历史，首轮进度会被漏掉）；② void submitTurn(...) 无
// catch，store 重抛时产生 unhandled rejection。
import { useEffect, useRef } from "react";
import { useResearchStore } from "../stores/researchStore";

export function useResearchSession(
  sessionId: string | undefined,
  pendingQuestion: string,
): void {
  const openSession = useResearchStore((s) => s.openSession);
  const connectStream = useResearchStore((s) => s.connectStream);
  const submitTurn = useResearchStore((s) => s.submitTurn);
  const streaming = useResearchStore((s) => s.streaming);

  const firstTurnSent = useRef(false);
  const wasStreaming = useRef(false);

  useEffect(() => {
    if (!sessionId) return;
    const controller = new AbortController();
    let disposed = false;

    void (async () => {
      await openSession(sessionId);
      if (disposed) return;

      // 等流建立（onOpen 在 fetch ok 且 body 存在后触发）再提交首轮。
      let streamOpened!: () => void;
      const opened = new Promise<void>((resolve) => {
        streamOpened = resolve;
      });
      void connectStream(sessionId, controller.signal, streamOpened);
      await opened;
      if (disposed) return;

      if (pendingQuestion && !firstTurnSent.current) {
        firstTurnSent.current = true;
        try {
          await submitTurn(sessionId, pendingQuestion);
        } catch {
          // store.submitTurn 已落 error，这里吞掉拒绝避免 unhandled rejection。
        }
      }
    })();

    return () => {
      disposed = true;
      controller.abort();
    };
  }, [sessionId, pendingQuestion, openSession, connectStream, submitTurn]);

  // 流结束（done / 终态 error）后重拉一轮历史，把完整 turn 时间线补进 store。
  useEffect(() => {
    if (!sessionId) return;
    if (wasStreaming.current && !streaming) {
      void openSession(sessionId);
    }
    wasStreaming.current = streaming;
  }, [sessionId, streaming, openSession]);
}
