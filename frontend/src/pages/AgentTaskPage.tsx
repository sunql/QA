import { useCallback, useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";
import {
  Alert,
  Button,
  Card,
  Input,
  InputNumber,
  Select,
  Space,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "../i18n";
import type { Vars } from "../i18n";
import { listModels } from "../api/modelConfig";
import {
  cancelAgentRun,
  createAgentRun,
  getAgentRun,
  getAgentRunSteps,
  listOrchestrations,
  resumeAgentRun,
} from "../api/agentOrchestrationApi";
import AgentRunTimeline from "../components/agent/AgentRunTimeline";
import type { ModelConfig } from "../types/modelConfig";
import type {
  AgentOrchestrationProfileRead,
  AgentRunRead,
  AgentRunResume,
  AgentRunStepRead,
  RunStatus,
} from "../types/agentOrchestration";

const { Title, Paragraph } = Typography;

/**
 * 轮询周期。与后端 Worker 的轮询周期（AGENT_ORCHESTRATION_POLL_SECONDS=2s）一致，
 * 因此 Worker 写入的步骤最多延迟约 2s 才出现在页面上——这是正常节奏，不是卡住。
 */
export const RUN_POLL_MS = 2000;

/** pauseDetail.budget 的取值：说明是哪一项预算被耗尽。 */
export type BudgetKey = "iteration" | "tool" | "cost" | "time";

const BUDGET_KEYS: readonly BudgetKey[] = ["iteration", "tool", "cost", "time"];

/** 后端把 axios 错误归一化成 AppError{code,status,detail}，没有 .response。 */
interface StatusError extends Error {
  status?: number;
}

type Translate = (key: string, vars?: Vars) => string;

/** 取 HTTP 状态码：拒绝值可能不是 Error，不能直接取属性。 */
function errorStatus(error: unknown): number | undefined {
  return error instanceof Error ? (error as StatusError).status : undefined;
}

/**
 * 取后端给出的诊断文本。AppError.message 的来源优先级：响应 detail 对象里的 message
 * → 响应体顶层 error → axios 兜底文案（见 api/client.ts 的 normalizeError）。
 */
function errorDetailText(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  return "";
}

/**
 * 把后端错误翻译成用户可读的提示。只按 status 分流——AppError.code 对所有
 * DomainError 都是 "UNKNOWN"，不可用于判别。
 */
function describeError(t: Translate, error: unknown): string {
  const status = errorStatus(error);
  const detail = errorDetailText(error);
  if (status === 404) return t("agentTask.messages.runNotFound");
  if (status === 403) return t("agentTask.messages.forbidden");
  if (status === 409) return t("agentTask.messages.conflict");
  if (status === 422) {
    return detail
      ? t("agentTask.messages.validationFailed", { detail })
      : t("agentTask.messages.validationFailedGeneric");
  }
  if (status === undefined || status === 0) {
    return t("agentTask.messages.networkError");
  }
  return t("agentTask.messages.requestFailed", { message: detail });
}

/** pauseDetail 是单个 budget 键，值为四种预算名之一。 */
function budgetKeyOf(run: AgentRunRead): BudgetKey | null {
  const raw = run.pauseDetail?.budget;
  return BUDGET_KEYS.includes(raw as BudgetKey) ? (raw as BudgetKey) : null;
}

/** 把预算增量写入 resume 载荷，字段名与后端 _BUDGET_INCREMENT_BY_BUDGET 一致。 */
function applyIncrement(
  payload: AgentRunResume,
  budget: BudgetKey,
  value: number,
): void {
  if (budget === "iteration") payload.additionalIterations = value;
  else if (budget === "tool") payload.additionalToolCalls = value;
  else if (budget === "cost") payload.additionalCostUsd = value;
  else payload.additionalTimeoutSeconds = value;
}

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
interface RunPollingHandlers {
  onRun: (run: AgentRunRead) => void;
  onSteps: (steps: readonly AgentRunStepRead[]) => void;
  onError: (error: unknown) => void;
  onMissing: () => void;
}

/**
 * 运行轮询 hook：只在 QUEUED / RUNNING 时按固定周期取运行快照与增量步骤。
 *
 * afterStepNo 是闭区间下界，因此每次都传 lastStepNo + 1，绝不重取最后一步。
 * 每次 effect 重启都会换一代次（generation）：旧的定时器链即使晚一拍恢复，
 * 也因代次不匹配而不再续期，避免两条并行的轮询链。终态与 PAUSED 都停止轮询；
 * 终态只补一次轨迹，让页面显示完整步骤。
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
    const shouldPoll = status === "QUEUED" || status === "RUNNING";

    const generation = generationRef.current + 1;
    generationRef.current = generation;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const isCurrent = (): boolean =>
      !cancelled && generationRef.current === generation;

    const fetchOnce = async (): Promise<void> => {
      try {
        const afterStepNo = lastStepNoRef.current + 1;
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
      void fetchOnce();
    }

    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [runId, status, isStopped, lastStepNoRef]);
}

/** 一次运行的跟踪状态与操作。 */
interface AgentRunTracker {
  run: AgentRunRead | null;
  steps: AgentRunStepRead[];
  actionError: string | null;
  /** 已请求取消但服务端尚未确认（运行仍在继续）。 */
  isCancelPending: boolean;
  openRun: (runId: string) => Promise<void>;
  cancelRun: () => Promise<void>;
  resumeRun: (payload: AgentRunResume) => Promise<void>;
  reportError: (message: string) => void;
  clearError: () => void;
}

/** 持有一次运行的全部状态，并提供不可变的步骤合并。 */
function useRunStore() {
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<AgentRunRead | null>(null);
  const [steps, setSteps] = useState<AgentRunStepRead[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pollStopped, setPollStopped] = useState(false);
  const [cancelRequested, setCancelRequested] = useState(false);

  /** 已持有的最后一条步骤号；下一次只取比它更新的步骤。 */
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
    runId, run, steps, actionError, pollStopped, cancelRequested,
    lastStepNoRef, setRun, setActionError, setCancelRequested, setPollStopped,
    mergeSteps, resetForRun,
  };
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
  storeRef: MutableRefObject<ReturnType<typeof useRunStore>>,
): RunActions {
  /** 调用一次后端动作，成功后把返回的运行快照写回 store。 */
  const callAction = useCallback(
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
function useAgentRunTracker(t: Translate): AgentRunTracker {
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
    run: store.run, steps: store.steps, actionError: store.actionError,
    isCancelPending, openRun: actions.openRun, cancelRun: actions.cancelRun,
    resumeRun: actions.resumeRun, reportError: store.setActionError, clearError,
  };
}

/** 创建运行表单的字段、提交动作与可选项。 */
interface CreateRunForm {
  profiles: AgentOrchestrationProfileRead[];
  models: ModelConfig[];
  agentCode: string | undefined;
  modelConfigId: number | undefined;
  input: string;
  creating: boolean;
  loadError: string | null;
  onAgentCodeChange: (value: string) => void;
  onModelChange: (value: number) => void;
  onInputChange: (value: string) => void;
  handleCreate: () => Promise<void>;
}

/**
 * 加载可用编排配置与启用中的模型，并处理「创建运行」。
 *
 * 只列出 enabled 的编排配置——停用的配置不该出现在任务页上。
 */
function useCreateRunForm(
  t: Translate,
  openRun: (runId: string) => Promise<void>,
  reportError: (message: string) => void,
  clearError: () => void,
): CreateRunForm {
  const tRef = useRef(t);
  tRef.current = t;

  const [profiles, setProfiles] = useState<AgentOrchestrationProfileRead[]>([]);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [agentCode, setAgentCode] = useState<string | undefined>(undefined);
  const [modelConfigId, setModelConfigId] = useState<number | undefined>(undefined);
  const [input, setInput] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const fail = (message: string): void => {
      if (!cancelled) setLoadError(tRef.current(message));
    };
    listOrchestrations()
      .then((rows) => {
        if (!cancelled) {
          setProfiles(rows.filter((row) => row.enabled === true));
        }
      })
      .catch(() => fail("agentTask.messages.loadProfilesFailed"));
    listModels(true)
      .then((rows) => {
        if (!cancelled) setModels(rows);
      })
      .catch(() => fail("agentTask.messages.loadModelsFailed"));
    return () => {
      cancelled = true;
    };
  }, []);

  const handleCreate = useCallback(async (): Promise<void> => {
    if (!agentCode) {
      reportError(t("agentTask.messages.profileRequired"));
      return;
    }
    if (modelConfigId === undefined) {
      reportError(t("agentTask.messages.modelRequired"));
      return;
    }
    const trimmed = input.trim();
    if (!trimmed) {
      reportError(t("agentTask.messages.inputRequired"));
      return;
    }
    setCreating(true);
    clearError();
    try {
      const created = await createAgentRun({
        agentCode,
        input: trimmed,
        modelConfigId,
      });
      await openRun(created.runId);
    } catch (error: unknown) {
      reportError(describeError(t, error));
    } finally {
      setCreating(false);
    }
  }, [agentCode, modelConfigId, input, t, openRun, reportError, clearError]);

  return {
    profiles, models, agentCode, modelConfigId, input, creating, loadError,
    onAgentCodeChange: setAgentCode, onModelChange: setModelConfigId,
    onInputChange: setInput, handleCreate,
  };
}

function CreateRunCard({ form }: { form: CreateRunForm }) {
  const { t } = useTranslation();
  const profileOptions = form.profiles.map((profile) => ({
    value: profile.agentCode,
    label: profile.agentCode,
  }));
  const modelOptions = form.models.map((model) => ({
    value: model.id,
    label: model.modelName,
  }));
  return (
    <Card size="small">
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Space wrap>
          <Select<string>
            value={form.agentCode}
            onChange={form.onAgentCodeChange}
            placeholder={t("agentTask.form.profilePlaceholder")}
            aria-label={t("agentTask.form.profileLabel")}
            style={{ width: 260 }}
            options={profileOptions}
          />
          <Select<number>
            value={form.modelConfigId}
            onChange={form.onModelChange}
            placeholder={t("agentTask.form.modelPlaceholder")}
            aria-label={t("agentTask.form.modelLabel")}
            style={{ width: 260 }}
            options={modelOptions}
          />
        </Space>
        <Input.TextArea
          value={form.input}
          onChange={(event) => form.onInputChange(event.target.value)}
          placeholder={t("agentTask.form.inputPlaceholder")}
          aria-label={t("agentTask.form.inputLabel")}
          autoSize={{ minRows: 2, maxRows: 6 }}
        />
        <Button
          type="primary"
          loading={form.creating}
          disabled={form.creating}
          onClick={() => void form.handleCreate()}
        >
          {t("agentTask.form.createButton")}
        </Button>
      </Space>
    </Card>
  );
}

interface RunStatusCardProps {
  run: AgentRunRead;
  isCancelPending: boolean;
  busy: boolean;
  onCancel: () => void;
}

function RunStatusCard(props: RunStatusCardProps) {
  const { t } = useTranslation();
  const { run } = props;
  const pauseCode = run.pauseCode ?? null;
  const isActive = run.status === "RUNNING" || run.status === "QUEUED";
  return (
    <Card size="small">
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Space wrap>
          <span>{t("agentTask.run.id", { runId: run.runId })}</span>
          <Tag>{t("agentTask.status." + run.status)}</Tag>
          {pauseCode ? (
            <Space size="small">
              <span>{t("agentTask.run.pauseReason")}</span>
              <Tag color="orange">{t("agentTask.pause." + pauseCode)}</Tag>
            </Space>
          ) : null}
        </Space>
        {props.isCancelPending ? (
          <Alert
            type="warning"
            showIcon
            message={t("agentTask.run.cancelRequested")}
          />
        ) : null}
        {isActive ? (
          <Button danger disabled={props.busy} onClick={props.onCancel}>
            {t("agentTask.run.cancelButton")}
          </Button>
        ) : null}
      </Space>
    </Card>
  );
}

/** 恢复 / 取消相关的表单状态与操作。 */
interface ResumeControls {
  acting: boolean;
  budgetKey: BudgetKey | null;
  supplementaryInput: string;
  incrementValue: number | null;
  onSupplementaryInputChange: (value: string) => void;
  onIncrementChange: (value: number | null) => void;
  handleResume: () => Promise<void>;
  handleCancel: () => Promise<void>;
}

/**
 * 组装恢复载荷：只有后端明确要求的字段才会被提交。
 *
 * 补充输入对应 NEEDS_INPUT / AMBIGUOUS；预算增量对应 BUDGET_EXHAUSTED，
 * 字段名由 pauseDetail.budget 决定。缺字段时先报错并返回 null。
 */
function buildResumePayload(
  t: Translate,
  run: AgentRunRead | null,
  supplementaryInput: string,
  increment: { budget: BudgetKey; value: number } | null,
  reportError: (message: string) => void,
): AgentRunResume | null {
  if (!run) return null;
  const pauseCode = run.pauseCode ?? null;
  const currentBudget = budgetKeyOf(run);
  const payload: AgentRunResume = {};

  if (pauseCode === "NEEDS_INPUT" || pauseCode === "AMBIGUOUS") {
    const trimmed = supplementaryInput.trim();
    if (!trimmed) {
      reportError(t("agentTask.messages.resumeInputRequired"));
      return null;
    }
    payload.input = trimmed;
  }
  if (pauseCode === "BUDGET_EXHAUSTED") {
    if (!currentBudget) {
      reportError(t("agentTask.messages.budgetDetailMissing"));
      return null;
    }
    if (!increment || increment.budget !== currentBudget) {
      reportError(t("agentTask.messages.resumeIncrementRequired"));
      return null;
    }
    applyIncrement(payload, currentBudget, increment.value);
  }
  return payload;
}

/** 组装恢复载荷并触发恢复 / 取消。 */
function useResumeControls(
  t: Translate,
  run: AgentRunRead | null,
  resumeRun: (payload: AgentRunResume) => Promise<void>,
  cancelRun: () => Promise<void>,
  reportError: (message: string) => void,
): ResumeControls {
  const [acting, setActing] = useState(false);
  const [supplementaryInput, setSupplementaryInput] = useState("");
  const [increment, setIncrement] = useState<{
    budget: BudgetKey;
    value: number;
  } | null>(null);

  const budgetKey = run ? budgetKeyOf(run) : null;

  // 切换到另一个运行时，旧的补充输入 / 预算增量不再适用。
  const runId = run?.runId ?? null;
  useEffect(() => {
    setSupplementaryInput("");
    setIncrement(null);
  }, [runId]);

  const handleResume = useCallback(async (): Promise<void> => {
    const payload = buildResumePayload(
      t,
      run,
      supplementaryInput,
      increment,
      reportError,
    );
    if (!payload) return;
    setActing(true);
    try {
      await resumeRun(payload);
      setSupplementaryInput("");
      setIncrement(null);
    } finally {
      setActing(false);
    }
  }, [run, supplementaryInput, increment, t, reportError, resumeRun]);

  const handleCancel = useCallback(async (): Promise<void> => {
    setActing(true);
    try {
      await cancelRun();
    } finally {
      setActing(false);
    }
  }, [cancelRun]);

  const incrementValue =
    increment && budgetKey && increment.budget === budgetKey
      ? increment.value
      : null;

  return {
    acting, budgetKey, supplementaryInput, incrementValue,
    onSupplementaryInputChange: setSupplementaryInput,
    onIncrementChange: (value: number | null) =>
      setIncrement(
        value === null || !budgetKey ? null : { budget: budgetKey, value },
      ),
    handleResume, handleCancel,
  };
}

interface ResumePanelProps {
  run: AgentRunRead;
  controls: ResumeControls;
}

function ResumePanel(props: ResumePanelProps) {
  const { t } = useTranslation();
  const { controls } = props;
  const pauseCode = props.run.pauseCode ?? null;
  const needsInput = pauseCode === "NEEDS_INPUT" || pauseCode === "AMBIGUOUS";
  const needsBudget = pauseCode === "BUDGET_EXHAUSTED";
  return (
    <Card size="small" title={t("agentTask.run.resumeTitle")}>
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        {needsInput ? (
          <Input.TextArea
            value={controls.supplementaryInput}
            onChange={(event) =>
              controls.onSupplementaryInputChange(event.target.value)
            }
            placeholder={t("agentTask.run.supplementaryInputPlaceholder")}
            aria-label={t("agentTask.run.supplementaryInputLabel")}
            autoSize={{ minRows: 2, maxRows: 6 }}
          />
        ) : null}
        {needsBudget ? (
          controls.budgetKey ? (
            <Space direction="vertical" size="small">
              <span>{t("agentTask.run.budgetTitle")}</span>
              <InputNumber
                value={controls.incrementValue ?? undefined}
                onChange={(value) => controls.onIncrementChange(value ?? null)}
                aria-label={t("agentTask.run.increment." + controls.budgetKey)}
                placeholder={t("agentTask.run.increment." + controls.budgetKey)}
              />
            </Space>
          ) : (
            <Alert
              type="warning"
              showIcon
              message={t("agentTask.messages.budgetDetailMissing")}
            />
          )
        ) : null}
        <Button
          type="primary"
          loading={controls.acting}
          disabled={controls.acting}
          onClick={() => void controls.handleResume()}
        >
          {t("agentTask.run.resumeButton")}
        </Button>
      </Space>
    </Card>
  );
}

/**
 * Agent 任务页：创建一次编排运行、逐步观察执行轨迹、暂停后恢复、运行中取消。
 *
 * 与旧 Chat/AgentRuntimePage 完全解耦：不复用 Chat 意图识别，也不做模型自动改派——
 * 用户选中的 modelConfigId 原样提交。
 */
export default function AgentTaskPage() {
  const { t } = useTranslation();

  const tracker = useAgentRunTracker(t);
  const {
    run,
    steps,
    actionError,
    isCancelPending,
    openRun,
    cancelRun,
    resumeRun,
    reportError,
    clearError,
  } = tracker;

  const form = useCreateRunForm(t, openRun, reportError, clearError);
  const resume = useResumeControls(t, run, resumeRun, cancelRun, reportError);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card size="small">
        <Title level={4} style={{ marginTop: 0 }}>
          {t("agentTask.title")}
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 12 }}>
          {t("agentTask.subtitle")}
        </Paragraph>
        <CreateRunCard form={form} />
      </Card>
      {form.loadError ? (
        <Alert type="error" showIcon message={form.loadError} />
      ) : null}
      {actionError ? <Alert type="error" showIcon message={actionError} /> : null}
      {run ? (
        <RunStatusCard
          run={run}
          isCancelPending={isCancelPending}
          busy={resume.acting}
          onCancel={() => void resume.handleCancel()}
        />
      ) : null}
      {run && run.status === "PAUSED" ? (
        <ResumePanel run={run} controls={resume} />
      ) : null}
      {run ? (
        <Card size="small">
          <AgentRunTimeline
            steps={steps}
            finalAnswer={run.finalAnswer ?? null}
            costSource={run.costSource}
            costComplete={run.costComplete}
          />
        </Card>
      ) : null}
    </Space>
  );
}
