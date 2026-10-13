// 研究会话页的流编排 hook（feat-research-entry 修复轮）。
//
// 串行编排 openSession → connectStream（等 onOpen）→ submitTurn，消除原实现的
// 两个缺陷：①「先建流后提交」靠 void 三连发、无 await，submitTurn 可能抢在 SSE
// 订阅建立前跑（后端不重放历史，首轮进度会被漏掉）；② void submitTurn(...) 无
// catch，store 重抛时产生 unhandled rejection。
import { useEffect, useRef } from "react";
import { useResearchStore } from "../stores/researchStore";

// 终态会话（与后端 `research_session_service.TERMINAL_SESSION_STATUSES` 同口径）。
// `awaiting_user` **不是**终态：会话在等用户答 checkpoint，状态机稍后续跑，流必须保持。
const TERMINAL_SESSION_STATUSES: ReadonlySet<string> = new Set(["done", "failed", "aborted"]);

/** 已结束的会话不连流（Task 12b）。后端流是长连接语义：`_eventFrames` 只在「回放非空且
 *  收尾于终态」时才 return，游标已最新的回放为空 ⇒ 实时循环只发心跳、永不返回，连了会
 *  让 UI 永远停在 streaming。终态会话的完整时间线由 openSession 的 turns 提供。
 *  只在 `openSession` 确实载入了**本会话**时判定，避免旧会话残留 status 误杀新流。 */
function isFinishedSession(sessionId: string): boolean {
  const session = useResearchStore.getState().currentSession;
  return session !== null && session.id === sessionId && TERMINAL_SESSION_STATUSES.has(session.status);
}

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

      // 已结束的会话不连流（Task 12b）。例外：带 pendingQuestion（新会话首轮）仍要连
      // —— 首轮提交后会话重新 running，进度得走这条流。
      if (!pendingQuestion && isFinishedSession(sessionId)) return;

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
