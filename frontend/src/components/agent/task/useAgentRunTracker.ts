import { useCallback, useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";
import {
  cancelAgentRun,
  getAgentRun,
  getAgentRunSteps,
  resumeAgentRun,
} from "../../../api/agentOrchestrationApi";
import type {
  AgentRunRead,
  AgentRunResume,
  AgentRunStepRead,
  RunStatus,
} from "../../../types/agentOrchestration";
import { describeError, errorStatus } from "./errors";
import type { Translate } from "./errors";
import type { AgentRunTracker } from "../../../types/agentTask";

/**
 * 轮询周期。与后端 Worker 的轮询周期（AGENT_ORCHESTRATION_POLL_SECONDS=2s）一致，
 * 因此 Worker 写入的步骤最多延迟约 2s 才出现在页面上——这是正常节奏，不是卡住。
 */
export const RUN_POLL_MS = 2000;

/**
 * 合并新步骤：按 stepNo 去重（后到的覆盖先到的）并按 stepNo 升序返回新数组。
 * 不修改入参，也不修改既有步骤对象。
 */
export function mergeStepLists(
  existing: readonly AgentRunStepRead[],
  incoming: readonly AgentRunStepRead[],
): AgentRunStepRead[] {
  if (incoming.length === 0) return [...existing];
  const byStepNo = new Map<number, AgentRunStepRead>();
  for (const step of existing) byStepNo.set(step.stepNo, step);
  for (const step of incoming) byStepNo.set(step.stepNo, step);
  return [...byStepNo.values()].sort((left, right) => left.stepNo - right.stepNo);
}

/** 轮询期回调：用 ref 持有最新值，避免它们进入 effect 依赖。 */
export interface RunPollingHandlers {
  onRun: (run: AgentRunRead) => void;
  onSteps: (steps: readonly AgentRunStepRead[]) => void;
  onError: (error: unknown) => void;
  onMissing: () => void;
}

/** 一次轮询会话所需的全部上下文。 */
interface PollSessionOptions {
  runId: string;
  /** 只有 QUEUED / RUNNING 才持续轮询；终态只补取一次轨迹。 */
  shouldPoll: boolean;
  /** 代次校验：effect 重启后，旧的定时器链必须失效。 */
  isGenerationCurrent: () => boolean;
  lastStepNoRef: MutableRefObject<number>;
  handlersRef: MutableRefObject<RunPollingHandlers>;
}

/**
 * 启动一条轮询链，返回停止函数。
 *
 * afterStepNo 是**闭区间下界**（后端取 step_no >= afterStepNo），所以这里传的是
 * lastStepNo **本身**，不是 lastStepNo + 1：后端会**原地改写**同一 stepNo 的行
 * （RUNNING → SUCCEEDED），排他游标会让这一行永远拉不回来——工具输出 / PLAN
 * 答案 / 步骤错误因此永远不可见。闭区间下界 + mergeStepLists 的「后到覆盖先到」
 * 让最后一行能被正确刷新（每轮多取一行，代价可忽略）。
 */
function startPolling(options: PollSessionOptions): () => void {
  const { runId, shouldPoll, lastStepNoRef, handlersRef } = options;
  let cancelled = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const isCurrent = (): boolean =>
    !cancelled && options.isGenerationCurrent();

  const fetchOnce = async (): Promise<void> => {
    try {
      const afterStepNo = Math.max(0, lastStepNoRef.current);
      const [read, incoming] = await Promise.all([
        getAgentRun(runId),
        getAgentRunSteps(runId, { afterStepNo }),
      ]);
      if (!isCurrent()) return;
      handlersRef.current.onRun(read);
      handlersRef.current.onSteps(incoming);
    } catch (error: unknown) {
      if (!isCurrent()) return;
      handlersRef.current.onError(error);
      // 运行不存在：停止轮询，但绝不伪造一个终态运行出来。
      if (errorStatus(error) === 404) handlersRef.current.onMissing();
    }
  };

  const tick = async (): Promise<void> => {
    await fetchOnce();
    if (isCurrent()) timer = setTimeout(() => void tick(), RUN_POLL_MS);
  };

  if (shouldPoll) {
    timer = setTimeout(() => void tick(), RUN_POLL_MS);
  } else {
    // 终态/暂停：补取一次轨迹。用的仍是**闭区间**游标 ⇒ 最后一行（可能被原地
    // 改写成终态）也能在这一轮被刷新。
    void fetchOnce();
  }

  return () => {
    cancelled = true;
    if (timer !== undefined) clearTimeout(timer);
  };
}

/**
 * 运行轮询 hook：只在 QUEUED / RUNNING 时按固定周期取运行快照与增量步骤。
 *
 * 每次 effect 重启都会换一代次：旧的定时器链即使晚一拍恢复，也因代次不匹配
 * 而不再续期，避免两条并行的轮询链。终态与 PAUSED 都停止轮询；终态只补一次
 * 轨迹，让页面显示完整步骤。
 */
function useRunPolling(
  runId: string | null,
  status: RunStatus | undefined,
  isStopped: boolean,
  lastStepNoRef: MutableRefObject<number>,
  handlers: RunPollingHandlers,
): void {
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  const generationRef = useRef(0);

  useEffect(() => {
    if (!runId || isStopped || status === undefined) return;
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    return startPolling({
      runId,
      shouldPoll: status === "QUEUED" || status === "RUNNING",
      isGenerationCurrent: () => generationRef.current === generation,
      lastStepNoRef,
      handlersRef,
    });
  }, [runId, status, isStopped, lastStepNoRef]);
}

/** 运行状态容器：持有当前运行、轨迹与页面级错误。 */
interface RunStore {
  runId: string | null;
  run: AgentRunRead | null;
  steps: AgentRunStepRead[];
  actionError: string | null;
  pollStopped: boolean;
  cancelRequested: boolean;
  lastStepNoRef: MutableRefObject<number>;
  setRun: (run: AgentRunRead | null) => void;
  setActionError: (message: string | null) => void;
  setCancelRequested: (value: boolean) => void;
  setPollStopped: (value: boolean) => void;
  mergeSteps: (incoming: readonly AgentRunStepRead[]) => void;
  resetForRun: (nextRunId: string) => void;
}

/** 持有一次运行的全部状态，并提供不可变的步骤合并。 */
function useRunStore(): RunStore {
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<AgentRunRead | null>(null);
  const [steps, setSteps] = useState<AgentRunStepRead[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pollStopped, setPollStopped] = useState(false);
  const [cancelRequested, setCancelRequested] = useState(false);

  /** 已持有的最后一条步骤号；下一次取「stepNo >= 它」的步骤（闭区间下界）。 */
  const lastStepNoRef = useRef(-1);

  /** 合并新到的步骤，并推进 lastStepNo 游标。 */
  const mergeSteps = useCallback((incoming: readonly AgentRunStepRead[]) => {
    if (incoming.length === 0) return;
    const highest = incoming.reduce(
      (max, step) => Math.max(max, step.stepNo),
      lastStepNoRef.current,
    );
    lastStepNoRef.current = highest;
    setSteps((previous) => mergeStepLists(previous, incoming));
  }, []);

  /** 切换到另一条运行：清空旧轨迹与旧错误。 */
  const resetForRun = useCallback((nextRunId: string) => {
    setRunId(nextRunId);
    setRun(null);
    setSteps([]);
    setActionError(null);
    setCancelRequested(false);
    setPollStopped(false);
    lastStepNoRef.current = -1;
  }, []);

  return {
    runId,
    run,
    steps,
    actionError,
    pollStopped,
    cancelRequested,
    lastStepNoRef,
    setRun,
    setActionError,
    setCancelRequested,
    setPollStopped,
    mergeSteps,
    resetForRun,
  };
}

/** 调用一次后端动作，成功后把返回的运行快照写回 store。 */
function useCallAction(
  t: Translate,
  storeRef: MutableRefObject<RunStore>,
): (
  action: () => Promise<AgentRunRead>,
  onSuccess?: () => void,
) => Promise<void> {
  return useCallback(
    async (
      action: () => Promise<AgentRunRead>,
      onSuccess?: () => void,
    ): Promise<void> => {
      const current = storeRef.current;
      current.setActionError(null);
      try {
        current.setRun(await action());
        if (onSuccess) onSuccess();
      } catch (error: unknown) {
        current.setActionError(describeError(t, error));
      }
    },
    [storeRef, t],
  );
}

/** 针对当前运行的三种操作：打开、取消、恢复。 */
interface RunActions {
  openRun: (runId: string) => Promise<void>;
  cancelRun: () => Promise<void>;
  resumeRun: (payload: AgentRunResume) => Promise<void>;
}

/** 打开 / 取消 / 恢复——都作用于 store 里当前选中的那条运行。 */
function useRunActions(
  t: Translate,
  storeRef: MutableRefObject<RunStore>,
): RunActions {
  const callAction = useCallAction(t, storeRef);

  /** 打开一次运行：重置轨迹并拉取运行快照。 */
  const openRun = useCallback(
    async (nextRunId: string): Promise<void> => {
      storeRef.current.resetForRun(nextRunId);
      await callAction(() => getAgentRun(nextRunId));
    },
    [storeRef, callAction],
  );

  const cancelRun = useCallback(async (): Promise<void> => {
    const runId = storeRef.current.runId;
    if (!runId) return;
    await callAction(
      () => cancelAgentRun(runId),
      () => storeRef.current.setCancelRequested(true),
    );
  }, [storeRef, callAction]);

  const resumeRun = useCallback(
    async (payload: AgentRunResume): Promise<void> => {
      const runId = storeRef.current.runId;
      if (!runId) return;
      await callAction(() => resumeAgentRun(runId, payload));
    },
    [storeRef, callAction],
  );

  return { openRun, cancelRun, resumeRun };
}

/**
 * 运行跟踪 hook：负责打开运行、轮询快照与增量步骤、取消与恢复。
 *
 * 轮询只在 QUEUED / RUNNING 时进行；终态与 PAUSED 都停止轮询（PAUSED 等待用户恢复）。
 */
export function useAgentRunTracker(t: Translate): AgentRunTracker {
  // t 每次渲染都是新闭包；放进 effect 依赖会形成无限重渲染，仓库统一用 ref 取最新值。
  const tRef = useRef(t);
  tRef.current = t;

  const store = useRunStore();
  // store 对象每次渲染都是新的；回调里读它时用 ref，避免把整包状态放进依赖。
  const storeRef = useRef(store);
  storeRef.current = store;

  useRunPolling(
    store.runId,
    store.run?.status,
    store.pollStopped,
    store.lastStepNoRef,
    {
      onRun: store.setRun,
      onSteps: store.mergeSteps,
      onError: (error: unknown) =>
        store.setActionError(describeError(tRef.current, error)),
      // 运行不存在：停止轮询，但绝不伪造一个终态运行出来。
      onMissing: () => store.setPollStopped(true),
    },
  );

  const actions = useRunActions(t, storeRef);

  const isCancelPending =
    store.run !== null &&
    (store.run.cancelRequested || store.cancelRequested) &&
    (store.run.status === "RUNNING" || store.run.status === "QUEUED");

  const clearError = useCallback(() => storeRef.current.setActionError(null), []);

  return {
    run: store.run,
    steps: store.steps,
    actionError: store.actionError,
    isCancelPending,
    openRun: actions.openRun,
    cancelRun: actions.cancelRun,
    resumeRun: actions.resumeRun,
    reportError: store.setActionError,
    clearError,
  };
}
