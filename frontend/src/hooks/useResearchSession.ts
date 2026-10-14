// 研究会话页的流编排 hook（feat-research-entry 修复轮）。
//
// 串行编排 openSession → connectStream（等 onOpen）→ submitTurn，消除原实现的
// 两个缺陷：①「先建流后提交」靠 void 三连发、无 await，submitTurn 可能抢在 SSE
// 订阅建立前跑（后端不重放历史，首轮进度会被漏掉）；② void submitTurn(...) 无
// catch，store 重抛时产生 unhandled rejection。
//
// Task 12b fix1：终态会话的「不连流」闸门只在挂载时判定一次，页内追问不会重跑
// effect ⇒ 追问前必须由调用方显式先建流（见 submitFollowUp）。
import { useCallback, useEffect, useRef } from "react";
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

export interface ResearchSessionHandle {
  /** 页内追问：**无活流时先建一条（等 onOpen）再提交**，已有活流则直接提交。
   *  不这样做的话，终态会话（挂载闸门不连流）上追问的新一轮事件没有 SSE 订阅者
   *  ⇒ 进度、完成通知、turns 全不更新，UI 卡死到整页刷新。 */
  submitFollowUp: (question: string) => Promise<void>;
}

interface StreamStarter {
  /** 建流并等 onOpen；已有活流/在途开流则复用同一条（去重）。
   *  返回值：`true` = 本次**新建**了流（connectStream 刚清空 error 槽，可据以判定
   *  本次开流成败）；`false` = 复用了已有活流（共享 error 槽里的值与本流无关，
   *  不得据此判定本次开流）。 */
  startStream: (sessionId: string) => Promise<boolean>;
  /** 断掉当前活流并清空句柄（组件卸载 / 切会话时调用，防泄漏）。 */
  abortStream: () => void;
}

/** connectStream 只在**流结束**时 resolve —— 若直接 await 会永久挂起。
 *  这里改成等 onOpen 的 promise；并用 ref 去重，避免挂载 effect 与 submitFollowUp
 *  并发双开（被 abort 的那条 onOpen 永不触发 ⇒ 挂起）。 */
function useStreamStarter(): StreamStarter {
  const connectStream = useResearchStore((s) => s.connectStream);
  // 活流（含在途开流）的控制器：用于「已有流不重开」去重与卸载 abort。
  const streamController = useRef<AbortController | null>(null);
  // 在途/活着那条流的 onOpen promise；结束后清空，允许下一次追问重开。
  const streamOpening = useRef<Promise<void> | null>(null);

  const startStream = useCallback(
    (sessionId: string): Promise<boolean> => {
      // 命中去重：复用现有活流，**不**新建 ⇒ false。此时 connectStream 的
      // set({ error: null }) 不会重跑，共享 error 槽可能是别的动作留下的陈旧值，
      // 调用方不得据此判定「本次开流失败」（fix3：否则复用活流会误拒追问）。
      if (streamOpening.current) return Promise.resolve(false);
      const controller = new AbortController();
      streamController.current = controller;
      // onOpen 信号（供调用方 await）与「流结束」分开：connectStream 的 promise 在
      // 流**结束**时才 settle，用它在结束点清句柄（早清会让追问双开、卸载不 abort）。
      let openResolve!: () => void;
      const opened = new Promise<void>((resolve) => {
        openResolve = resolve;
      });
      const ended = connectStream(sessionId, controller.signal, openResolve);
      streamOpening.current = opened;
      void ended
        .catch(() => undefined)
        .then(() => {
          if (streamOpening.current === opened) {
            streamOpening.current = null;
            streamController.current = null;
          }
        });
      // 新开路径可靠：connectStream(:369) 刚清空 error，读点是下一微任务，
      // 无并发写缝 ⇒ 本次开流结果就是 error 槽的当前值。
      return opened.then(() => true);
    },
    [connectStream],
  );

  const abortStream = useCallback((): void => {
    streamController.current?.abort();
    streamController.current = null;
    streamOpening.current = null;
  }, []);

  return { startStream, abortStream };
}

export function useResearchSession(
  sessionId: string | undefined,
  pendingQuestion: string,
): ResearchSessionHandle {
  const openSession = useResearchStore((s) => s.openSession);
  const submitTurn = useResearchStore((s) => s.submitTurn);
  const streaming = useResearchStore((s) => s.streaming);
  const { startStream, abortStream } = useStreamStarter();

  const firstTurnSent = useRef(false);
  const wasStreaming = useRef(false);

  const submitFollowUp = useCallback(
    async (question: string): Promise<void> => {
      if (!sessionId) return;
      // 先建流（等 onOpen）再提交：顺序颠倒会让首帧事件没有订阅者。
      // fix3：守卫**只对本次新开**的流生效（openedFresh）。复用活流时 error 槽可能是
      // 陈旧值（本流建立时清的空，之后别的动作把它置脏），无条件判 error 会误拒追问。
      const openedFresh = await startStream(sessionId);
      // Task 12b fix2：connectStream 的失败分支**刻意**先 signalOpen（兜底触发 onOpen，
      // 防「先等 onOpen 再提交」的调用方永久挂起）再 set({ error })，故开流失败时
      // startStream 也会 resolve。新开路径必须拦截，否则 submitTurn 首行 set({ error: null })
      // 会抹掉开流错误 ⇒ 追问照提交、无 SSE 订阅者、无任何提示（静默失败）。
      // 时序确定：signalOpen 同步 resolve 了 opened，但 await 续体是微任务，晚于 catch
      // 里后续同步的 set({ error }) ⇒ 读到的必是本次开流结果（成功路径 error 保持 null）。
      if (openedFresh) {
        const streamError = useResearchStore.getState().error;
        if (streamError !== null) {
          throw new Error(streamError);
        }
      }
      await submitTurn(sessionId, question);
    },
    [sessionId, startStream, submitTurn],
  );

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;

    void (async () => {
      await openSession(sessionId);
      if (cancelled) return;

      // 已结束的会话不连流（Task 12b）。例外：带 pendingQuestion（新会话首轮）仍要连
      // —— 首轮提交后会话重新 running，进度得走这条流。
      if (!pendingQuestion && isFinishedSession(sessionId)) return;

      // 等流建立（onOpen 在 fetch ok 且 body 存在后触发）再提交首轮。
      // 刻意忽略 startStream 的返回值：挂载首轮路径语义不变（fix3 只圈 submitFollowUp，
      // 该路径「开流失败仍静默提交」为已登记的既有问题，见 brief MEDIUM-1）。
      await startStream(sessionId);
      if (cancelled) return;

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
      cancelled = true;
      // 页内追问建的流也必须随页面卸载释放（挂载 effect 建的流同理）。
      abortStream();
    };
  }, [sessionId, pendingQuestion, openSession, startStream, abortStream, submitTurn]);

  // 流结束（done / 终态 error）后重拉一轮历史，把完整 turn 时间线补进 store。
  useEffect(() => {
    if (!sessionId) return;
    if (wasStreaming.current && !streaming) {
      void openSession(sessionId);
    }
    wasStreaming.current = streaming;
  }, [sessionId, streaming, openSession]);

  return { submitFollowUp };
}
