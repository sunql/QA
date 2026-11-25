/** I3 采购 Copilot 页（设计稿 §8.4 的统一对话入口 / M4-2 前端面）。
 *
 * P4 场景配置化后的口径：
 * - **场景卡片墙**来自 `GET /procurement/copilot/scenarios`（DB 配置；
 *   加场景 = 插一行配置，前端零改动）；自由对话卡固定追加（走通用目录）；
 * - **场景工作区**的槽位表单按 slotSchema 动态生成；推荐 chips 来自配置；
 * - **提问组装在服务端**：前端只传 scenarioCode + 结构化 slots + 用户文字，
 *   冲突让位/模板替换在后端（改配置即时生效，不依赖前端发版）；
 * - **模型**必须显式选择（后端必填 modelConfigId，且不会替你改派）。
 *
 * 暂停（PAUSED）不是失败：多候选/缺参数时 Agent 会 ASK_USER，
 * 页面把它渲染成「需要你补充」的追问，用户接着发下一条即可。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Input,
  message,
  Select,
  Space,
} from "antd";
import { LeftOutlined } from "@ant-design/icons";
import { useTranslation } from "../i18n";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "../stores/authStore";
import ConversationHistoryPanel from "../components/copilot/ConversationHistoryPanel";
import { parsePendingDeliveryStep } from "../components/copilot/PendingDeliveryResultCard";
import { parseSupplierRiskStep } from "../components/copilot/SupplierRiskResultCard";
import PendingDeliveryResultTabs from "../components/copilot/PendingDeliveryResultTabs";
import CopilotScenarioCards from "../components/copilot/CopilotScenarioCards";
import CopilotTurnThread from "../components/copilot/CopilotTurnThread";
import PendingDeliveryWelcome from "../components/copilot/PendingDeliveryWelcome";
import ScenarioAdvancedParams, {
  hasAdvancedFilters,
  type DateMode,
  type SlotValues,
} from "../components/copilot/ScenarioAdvancedParams";
import HistoryToggleButton from "../components/chat/HistoryToggleButton";
import {
  EMPTY_TRACE,
  restoredStep,
  restoredTurn,
  type CopilotTurn,
  type TraceState,
} from "../components/copilot/copilotTurnModel";
import { getAgentCatalog, type AgentCatalogEntry } from "../api/procurementKnowledge";
import { listModels } from "../api/modelConfig";
import { listDataSources } from "../api/datasource";
import {
  listCopilotScenarios,
  type CopilotScenario,
  type CopilotScenarioChip,
} from "../api/copilotScenario";
import {
  deleteCopilotConversation,
  getCopilotConversation,
  getPendingDeliverySummary,
  listCopilotConversations,
  listPendingDeliverySites,
  pinCopilotConversation,
  streamCopilotMessage,
  type CopilotConversationSummary,
  type CopilotDonePayload,
  type PendingDeliverySummaryResponse,
} from "../api/procurementCopilot";
import { getAgentRunSteps } from "../api/agentOrchestrationApi";
import type { DataSource } from "../types/datasource";
import type { ModelConfig } from "../types/modelConfig";

const { TextArea } = Input;
/** 待到货场景编码（欢迎态统计/结果跳转按它识别；其余场景通用渲染）。 */
const PENDING_DELIVERY_CODE = "pending_delivery";
/** 欢迎态统计的默认时间窗（月）。 */
const DEFAULT_MONTHS_BACK = 12;

/** 页面两级视图：home = 场景卡片墙；进入场景/自由对话后才是对话工作区。 */
type CopilotView =
  | { kind: "home" }
  | { kind: "free" }
  | { kind: "scenario"; scenario: CopilotScenario };

/** 槽位默认值：monthsBack 12（时间窗兜底）、多值 []、其余留空。
 *
 * 非 monthsBack 的数字槽位**不**预填 0：后端把 0 视为「显式指定」
 * （如 anchorDay=0 会劫持取数窗口并触发校验失败），0 还会让空表单被
 * `hasAdvancedFilters` 误判为「已改高级参数」。留空 ⇒ JSON 上送时该键
 * 直接缺席，后端按 None 跳过。
 */
export function defaultSlotValues(scenario: CopilotScenario): SlotValues {
  const values: SlotValues = {};
  for (const slot of scenario.slotSchema) {
    if (slot.key === "monthsBack") values[slot.key] = DEFAULT_MONTHS_BACK;
    else if (slot.type === "entityMulti" || slot.type === "dictMulti") values[slot.key] = [];
  }
  return values;
}

/** chip 预设 → 直接上送的 slots：默认值打底、chip 覆盖，丢弃 undefined。 */
export function buildChipSlots(
  scenario: CopilotScenario,
  chip: CopilotScenarioChip,
): Record<string, unknown> {
  const { label: _label, ...chipSlots } = chip;
  return Object.fromEntries(
    Object.entries({ ...defaultSlotValues(scenario), ...chipSlots }).filter(
      ([, value]) => value !== undefined,
    ),
  );
}

export default function ProcurementCopilotPage() {
  const { t } = useTranslation();
  const nav = useNavigate();
  // 数据源切换是治理动作（可指向别的业务库）⇒ 仅 admin 可见（2026-11-24 需求）
  const user = useAuthStore((s) => s.user);
  const isAdmin = (user?.roles ?? []).includes("admin");
  const [scenarioConfigs, setScenarioConfigs] = useState<CopilotScenario[]>([]);
  /** 自由对话用的通用 Agent 目录（只列 runnable）。 */
  const [scenarios, setScenarios] = useState<AgentCatalogEntry[]>([]);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [agentCode, setAgentCode] = useState<string | undefined>();
  const [modelId, setModelId] = useState<number | undefined>();
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<CopilotTurn[]>([]);
  const [busy, setBusy] = useState(false);
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [loadError, setLoadError] = useState<string | null>(null);
  const [trace, setTrace] = useState<TraceState>(EMPTY_TRACE);
  const [conversations, setConversations] = useState<CopilotConversationSummary[]>([]);
  /** 置顶会话（P4 阶段5：pinned_at 落库跨设备同步；本端只读服务端状态）。 */
  const pinnedIds = useMemo(
    () =>
      conversations
        .filter((item) => Boolean(item.pinnedAt))
        .map((item) => item.conversationId),
    [conversations],
  );
  // 历史侧栏默认收起（2026-11-24 反馈）：聚焦当前对话，历史从右侧浮钮展开
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(true);
  const abortRef = useRef<AbortController | null>(null);

  const loadOptions = useCallback(async () => {
    setLoadError(null);
    try {
      const [catalog, modelRows, dsRows, scenarioRows] = await Promise.all([
        getAgentCatalog(),
        listModels(true),
        listDataSources(true),
        listCopilotScenarios(),
      ]);
      const runnable = catalog.filter((entry) => entry.runnable);
      setScenarios(runnable);
      setModels(modelRows);
      setScenarioConfigs(scenarioRows);
      setDatasources(Array.isArray(dsRows) ? dsRows : []);
      setDatasourceId(
        (prev) =>
          prev ??
          (Array.isArray(dsRows) ? dsRows.find((d) => d.isDefault)?.id ?? dsRows[0]?.id : undefined),
      );
      setAgentCode((prev) => prev ?? runnable[0]?.agentCode);
      setModelId((prev) => prev ?? modelRows[0]?.id);
    } catch (err) {
      setLoadError(
        err instanceof Error ? err.message : t("procurement.copilot.loadFailed"),
      );
    }
  }, [t]);

  useEffect(() => {
    void loadOptions();
  }, [loadOptions]);

  /** 终态回读：done 到达后拉一次完整步骤（含 outputJson），替换流式中间态。 */
  const refreshSteps = useCallback(async (turnId: string, runId: string) => {
    try {
      // 映射必须在 updater **外**完成：setTurns 的 updater 由 React 在渲染期
      // 惰性执行，里面抛错 try/catch 包不住 ⇒ 整棵树崩（实测 2026-11-23）。
      const mapped = (await getAgentRunSteps(runId)).map(restoredStep);
      setTurns((prev) =>
        prev.map((turn) =>
          turn.id === turnId ? { ...turn, steps: mapped } : turn,
        ),
      );
    } catch {
      /* 回读失败保留流式中间态（轨迹抽屉仍可现拉） */
    }
  }, []);

  /** 侧栏历史：拿不到就不显示，不阻断对话主流程。 */
  const loadConversations = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const rows = await listCopilotConversations();
      setConversations(Array.isArray(rows) ? rows : []);
    } catch {
      /* 列表失败静默：对话本身仍可用 */
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  const togglePin = useCallback(
    (id: string) => {
      const isPinned = pinnedIds.includes(id);
      void pinCopilotConversation(id, !isPinned)
        .then(() => loadConversations())
        .catch(() => {
          // 旧后端无 pin 端点（1124-fix04 仅发前端）⇒ 友好提示而非裸 404
          message.warning(t("procurement.copilot.pinUnavailable"));
        });
    },
    [pinnedIds, loadConversations, t],
  );

  /** 选中历史会话：恢复轮次、场景与模型（Agent/模型绑定在会话上）。 */
  const restoreConversation = useCallback(async (id: string) => {
    try {
      const detail = await getCopilotConversation(id);
      setConversationId(detail.conversationId);
      setAgentCode((prev) => prev ?? detail.agentCode);
      setModelId((prev) => prev ?? detail.modelConfigId);
      setTurns(detail.turns.map((row) => restoredTurn(row, detail.agentCode)));
    } catch {
      // 会话不存在/无权访问（换账号或已删除）：清空线程，保持当前视图
      setConversationId(undefined);
    }
  }, []);

  // 打开页面：只拉历史列表。**不自动恢复**上次会话 —— 参考系统口径：
  // 永远落在场景卡片墙，历史线程从侧栏一键恢复。
  useEffect(() => {
    void loadConversations();
  }, [loadConversations]);

  /** 删除会话：删当前会话时一并清空线程；随后刷新侧栏。 */
  const handleDeleteConversation = useCallback(
    async (id: string) => {
      try {
        await deleteCopilotConversation(id);
      } catch {
        // 旧后端无 DELETE 端点（1124-fix02 仅发前端）⇒ 友好提示而非裸 404
        message.warning(t("procurement.copilot.deleteUnavailable"));
        return;
      }
      if (id === conversationId) {
        setTurns([]);
        setConversationId(undefined);
      }
      await loadConversations();
    },
    [conversationId, loadConversations, t],
  );

  useEffect(() => () => abortRef.current?.abort(), []);

  const updateTurn = (id: string, patch: Partial<CopilotTurn>) => {
    setTurns((prev) =>
      prev.map((turn) => (turn.id === id ? { ...turn, ...patch } : turn)),
    );
  };

  /**
   * 打开执行轨迹。必须**带 Authorization 拉取**再在页内渲染：
   * 新窗口的裸导航带不上 Bearer 头（2026-11-23 生产实证）。
   */
  const openTrace = async (runId: string) => {
    setTrace({ runId, loading: true, error: null, steps: [] });
    try {
      const steps = await getAgentRunSteps(runId);
      setTrace((prev) =>
        prev.runId === runId ? { ...prev, loading: false, steps } : prev,
      );
    } catch (err) {
      setTrace((prev) =>
        prev.runId === runId
          ? {
              ...prev,
              loading: false,
              error:
                err instanceof Error
                  ? err.message
                  : t("procurement.copilot.traceFailed"),
            }
          : prev,
      );
    }
  };

  const [view, setView] = useState<CopilotView>({ kind: "home" });
  const isScenarioView = view.kind === "scenario";
  const activeScenario = isScenarioView ? view.scenario : null;
  const isPendingDelivery = activeScenario?.scenarioCode === PENDING_DELIVERY_CODE;

  const [showAdvanced, setShowAdvanced] = useState(false);
  const [activeResultTurnId, setActiveResultTurnId] = useState<string | null>(null);
  const [summaryState, setSummaryState] = useState<{
    loading: boolean;
    error: string | null;
    data: PendingDeliverySummaryResponse | null;
  }>({ loading: false, error: null, data: null });

  /** agentCode → 场景显示名（历史侧栏分组用）。 */
  const agentNames = useMemo(() => {
    const map: Record<string, string> = {};
    for (const entry of scenarios) map[entry.agentCode] = entry.agentName;
    for (const scenario of scenarioConfigs) map[scenario.agentCode] = scenario.name;
    return map;
  }, [scenarios, scenarioConfigs]);

  /** 有专用结果渲染器的场景（其余场景走线程 Markdown 答案）。 */
  const structuredRenderer: "pending_delivery" | "supplier_risk" | null =
    activeScenario?.resultRenderer === "supplier_risk"
      ? "supplier_risk"
      : activeScenario?.resultRenderer === "pending_delivery" || isPendingDelivery
        ? "pending_delivery"
        : null;
  /** 会话内带结构化结果的轮次（顺序）——多结果用 Tabs 切换，不再纵向堆叠文字墙。
   *  解析器按场景渲染器分派（每步只匹配各自工具的 observation）。 */
  const resultTurns = turns
    .map((turn) => ({
      turn,
      parsed:
        turn.steps
          .map((step) =>
            structuredRenderer === "supplier_risk"
              ? parseSupplierRiskStep(step)
              : parsePendingDeliveryStep(step),
          )
          .find((parsed) => parsed !== null) ?? null,
    }))
    .filter((item) => item.parsed !== null);

  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [datasourceId, setDatasourceId] = useState<number | undefined>();
  /** 结构化槽位值（键 = slotSchema[].key；发消息时随 scenarioCode 上送）。 */
  const [slotValues, setSlotValues] = useState<SlotValues>({});
  const [dateMode, setDateMode] = useState<DateMode>("months");

  /** 进入场景（卡片墙 → 工作区）：整体重置，跨会话不串扰。 */
  const resetWorkspace = useCallback(() => {
    setTurns([]);
    setConversationId(undefined);
    setActiveResultTurnId(null);
    setShowAdvanced(false);
    setDateMode("months");
    setQuestion("");
  }, []);

  const enterScenario = useCallback(
    (scenario: CopilotScenario) => {
      resetWorkspace();
      setSlotValues(defaultSlotValues(scenario));
      setView({ kind: "scenario", scenario });
    },
    [resetWorkspace],
  );

  const enterFree = useCallback(() => {
    resetWorkspace();
    setSlotValues({});
    setView({ kind: "free" });
  }, [resetWorkspace]);

  const setSlot = useCallback((key: string, value: unknown) => {
    setSlotValues((prev) => ({ ...prev, [key]: value }));
  }, []);

  const [siteOptions, setSiteOptions] = useState<{ code: string; name: string }[]>([]);

  /** 基地选项：进场景/换数据源拉一次。 */
  useEffect(() => {
    if (!isPendingDelivery) return;
    listPendingDeliverySites(datasourceId)
      .then((rows) => setSiteOptions(Array.isArray(rows) ? rows : []))
      .catch(() => setSiteOptions([]));
  }, [isPendingDelivery, datasourceId]);

  /** 欢迎态业务现状：进入场景/换数据源/换时间窗即拉取（零 LLM 成本）。 */
  const summaryFilters = useMemo(
    () => ({
      datasourceId,
      monthsBack: Number(slotValues.monthsBack ?? DEFAULT_MONTHS_BACK),
      supplierKeys: Array.isArray(slotValues.supplierKeys) ? (slotValues.supplierKeys as string[]) : [],
      materialKeys: Array.isArray(slotValues.materialKeys) ? (slotValues.materialKeys as string[]) : [],
      siteKeys: Array.isArray(slotValues.siteKeys) ? (slotValues.siteKeys as string[]) : [],
      dateFrom: typeof slotValues.dateFrom === "string" ? slotValues.dateFrom : undefined,
      dateTo: typeof slotValues.dateTo === "string" ? slotValues.dateTo : undefined,
    }),
    [datasourceId, slotValues],
  );

  useEffect(() => {
    if (!isPendingDelivery) return;
    let cancelled = false;
    setSummaryState((prev) => ({ ...prev, loading: true, error: null }));
    getPendingDeliverySummary(summaryFilters)
      .then((data) => {
        if (!cancelled) setSummaryState({ loading: false, error: null, data });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setSummaryState({
            loading: false,
            error: err instanceof Error ? err.message : "统计加载失败",
            data: null,
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [isPendingDelivery, summaryFilters]);

  /** 组装场景分支的 slots：当前槽位值 + admin 数据源（治理字段非 admin 不传）。 */
  const buildScenarioSlots = useCallback((): Record<string, unknown> | undefined => {
    if (!activeScenario) return undefined;
    const slots: Record<string, unknown> = { ...slotValues };
    // 时间窗「月份」模式下不传显式区间（二选一）
    if (dateMode === "months") {
      delete slots.dateFrom;
      delete slots.dateTo;
    }
    if (isAdmin && datasourceId !== undefined) slots.datasourceId = datasourceId;
    return slots;
  }, [activeScenario, slotValues, dateMode, isAdmin, datasourceId]);

  /**
   * 发起一轮对话。场景分支 = scenarioCode + slots（主句可空，由模板提供）；
   * 自由分支 = 纯文字。displayQuestion 仅用于界面展示（wire 上 message 可为空）。
   */
  const sendTurn = async (opts: {
    message: string;
    displayQuestion: string;
    agentCode: string;
    scenarioCode?: string;
    slots?: Record<string, unknown>;
  }) => {
    const { message, displayQuestion, agentCode: turnAgent } = opts;
    if (busy || !modelId) return;
    if (!message.trim() && !opts.slots) return;
    // 场景会钉死自己的 Agent：与当前会话绑定不一致 ⇒ 后端 422（会话绑定 Agent），
    // 所以这里换 Agent 时必须开新会话（清 conversationId）。
    const effectiveConversation = turnAgent !== agentCode ? undefined : conversationId;
    if (turnAgent !== agentCode) {
      setAgentCode(turnAgent);
      setConversationId(undefined);
    }
    const id = `turn-${Date.now()}`;
    setTurns((prev) => [
      ...prev,
      {
        id,
        question: displayQuestion,
        createdAt: Date.now(),
        agentCode: turnAgent,
        resumed: undefined,
        runId: null,
        steps: [],
        status: "RUNNING",
        answer: null,
        pauseCode: null,
        error: null,
        totalTokens: 0,
      },
    ]);
    setQuestion("");
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamCopilotMessage(
        {
          message: message.trim(),
          agentCode: turnAgent,
          modelConfigId: modelId,
          ...(opts.scenarioCode ? { scenarioCode: opts.scenarioCode, slots: opts.slots ?? {} } : {}),
          ...(effectiveConversation ? { conversationId: effectiveConversation } : {}),
        },
        {
          onRun: (run) => {
            if (run.conversationId) {
              setConversationId(run.conversationId);
              void loadConversations();
            }
            updateTurn(id, {
              runId: run.runId,
              resumed: run.resumed,
            });
          },
          onStep: (step) =>
            setTurns((prev) =>
              prev.map((turn) =>
                turn.id === id ? { ...turn, steps: [...turn.steps, step] } : turn,
              ),
            ),
          onDone: (done: CopilotDonePayload) => {
            updateTurn(id, {
              status: "DONE",
              answer: done.answer,
              pauseCode: done.pauseCode,
              totalTokens: done.totalTokens,
            });
            void refreshSteps(id, done.runId);
          },
          onError: (err) =>
            updateTurn(id, { status: "ERROR", error: err.error }),
        },
        { signal: controller.signal, fallbackError: t("procurement.copilot.sendFailed") },
      );
    } catch (err) {
      if (!controller.signal.aborted) {
        const errorMessage =
          err instanceof Error ? err.message : t("procurement.copilot.sendFailed");
        // 只覆盖「还在跑」的轮次：若已收到终态事件（done / timeout 等），
        // 之后 nginx 重置连接导致的 TypeError 不应把有意义的文案冲掉
        // （2026-11-23：ERR_INCOMPLETE_CHUNKED_ENCODING 覆盖了超时提示）。
        setTurns((prev) =>
          prev.map((turn) =>
            turn.id === id && turn.status === "RUNNING"
              ? { ...turn, status: "ERROR", error: errorMessage }
              : turn,
          ),
        );
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  };

  /** 场景输入框发送：文字为主句（可空 ⇒ 模板兜底），槽位随 request 上送。 */
  const sendScenarioQuestion = useCallback(() => {
    if (!activeScenario) return;
    const text = question.trim();
    if (!text && !hasAdvancedFilters(slotValues)) return;
    void sendTurn({
      message: text,
      displayQuestion: text || activeScenario.questionTemplate || activeScenario.name,
      agentCode: activeScenario.agentCode,
      scenarioCode: activeScenario.scenarioCode,
      slots: buildScenarioSlots(),
    });
  }, [activeScenario, question, slotValues, buildScenarioSlots]);

  /** chip 发送：预设槽位组合直接上送，message 为空（主句由模板提供）。 */
  const sendScenarioChip = useCallback(
    (chip: CopilotScenarioChip) => {
      if (!activeScenario) return;
      const slots = buildChipSlots(activeScenario, chip);
      if (isAdmin && datasourceId !== undefined) slots.datasourceId = datasourceId;
      void sendTurn({
        message: "",
        displayQuestion: chip.label,
        agentCode: activeScenario.agentCode,
        scenarioCode: activeScenario.scenarioCode,
        slots,
      });
    },
    [activeScenario, isAdmin, datasourceId],
  );

  return (
    <div style={{ display: "flex", height: "100%", position: "relative" }}>
      <div style={{ flex: 1, minWidth: 0, padding: 24, overflowY: "auto" }}>
      <Space align="center" style={{ marginBottom: 12 }}>
        {view.kind !== "home" && (
          <Button icon={<LeftOutlined />} onClick={() => setView({ kind: "home" })}>
            {t("procurement.copilot.backToScenarios")}
          </Button>
        )}
        <h2 style={{ margin: 0 }}>
          {view.kind === "home"
            ? t("procurement.copilot.title")
            : `${t("procurement.copilot.title")} · ${
                view.kind === "scenario" ? view.scenario.name : t("procurement.copilot.scenarioFree")
              }`}
        </h2>
      </Space>
      {view.kind === "home" ? (
        <CopilotScenarioCards
          scenarios={scenarioConfigs}
          onEnterScenario={enterScenario}
          onEnterFree={enterFree}
        />
      ) : (
        <>
      {isPendingDelivery && turns.length === 0 && (
        <PendingDeliveryWelcome
          summary={summaryState}
          chips={activeScenario?.chips ?? []}
          disabled={busy || !modelId}
          onChipClick={sendScenarioChip}
        />
      )}
      {isScenarioView && (
        <Card
          size="small"
          style={{
            marginBottom: 16,
            border: "1.5px solid #91caff",
            borderRadius: 10,
            background: "#f7faff",
          }}
        >
          <TextArea
            rows={2}
            value={question}
            placeholder={t("procurement.copilot.scenarioInputPlaceholder")}
            onChange={(event) => setQuestion(event.target.value)}
            onPressEnter={(event) => {
              // 组合键与按钮同口径：有文字或改过高级参数均可发送
              if (
                (event.ctrlKey || event.metaKey) &&
                (question.trim() || hasAdvancedFilters(slotValues))
              ) {
                sendScenarioQuestion();
              }
            }}
          />
          <Space style={{ marginTop: 8 }}>
            <Button
              type="primary"
              loading={busy}
              disabled={busy || !modelId || (!question.trim() && !hasAdvancedFilters(slotValues))}
              onClick={sendScenarioQuestion}
            >
              {t("procurement.copilot.send")}
            </Button>
            <Button onClick={() => setShowAdvanced((prev) => !prev)}>
              {showAdvanced
                ? t("procurement.copilot.hideAdvanced")
                : t("procurement.copilot.advancedParams")}
            </Button>
          </Space>
        </Card>
      )}
      {isScenarioView && showAdvanced && activeScenario && (
        <ScenarioAdvancedParams
          slots={activeScenario.slotSchema}
          values={slotValues}
          onChange={setSlot}
          dateMode={dateMode}
          onDateModeChange={setDateMode}
          models={models}
          modelId={modelId}
          onModelChange={setModelId}
          isAdmin={isAdmin}
          datasources={datasources}
          datasourceId={datasourceId}
          onDatasourceChange={setDatasourceId}
          siteOptions={siteOptions}
        />
      )}
      {view.kind === "free" && (
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={t("procurement.copilot.singleTurnHint")}
      />
      )}
      {view.kind === "free" && (
      <Card
        size="small"
        style={{
          marginBottom: 16,
          border: "1.5px solid #91caff",
          borderRadius: 10,
          background: "#f7faff",
        }}
      >
        <Space wrap>
          <Select
            style={{ minWidth: 280 }}
            placeholder={t("procurement.copilot.scenario")}
            value={agentCode}
            onChange={setAgentCode}
            options={scenarios.map((entry) => ({
              value: entry.agentCode,
              label: entry.agentName,
            }))}
          />
          <Select
            style={{ minWidth: 220 }}
            placeholder={t("procurement.copilot.model")}
            value={modelId}
            onChange={setModelId}
            options={models.map((model) => ({
              value: model.id,
              label: model.modelName,
            }))}
          />
        </Space>
        <div style={{ marginTop: 12 }}>
          <TextArea
            rows={3}
            value={question}
            placeholder={t("procurement.copilot.placeholder")}
            onChange={(event) => setQuestion(event.target.value)}
            onPressEnter={(event) => {
              if (event.ctrlKey || event.metaKey)
                void sendTurn({
                  message: question,
                  displayQuestion: question,
                  agentCode: agentCode ?? "",
                });
            }}
          />
          <Space style={{ marginTop: 8 }}>
            <Button
              type="primary"
              loading={busy}
              disabled={!question.trim() || !agentCode || !modelId}
              onClick={() =>
                void sendTurn({
                  message: question,
                  displayQuestion: question,
                  agentCode: agentCode ?? "",
                })
              }
            >
              {t("procurement.copilot.send")}
            </Button>
            <Button
              disabled={busy || turns.length === 0}
              onClick={() => {
                setTurns([]);
                setConversationId(undefined);
              }}
            >
              {t("procurement.copilot.newConversation")}
            </Button>
          </Space>
        </div>
      </Card>
      )}
      {loadError && (
        <Alert type="error" showIcon message={loadError} style={{ marginBottom: 12 }} />
      )}
        </>
      )
      }
      {/* 会话内多结果：Tabs 仅作切换器，内容只渲染激活项（滚动不会看到其他结果） */}
      {structuredRenderer && resultTurns.length > 0 && (
        <PendingDeliveryResultTabs
          renderer={structuredRenderer}
          resultTurns={resultTurns}
          activeResultTurnId={activeResultTurnId}
          onChange={setActiveResultTurnId}
          onViewLines={() => void nav("/procurement/pending-delivery")}
        />
      )}
      <CopilotTurnThread
        visible={view.kind !== "home"}
        showEmptyState={view.kind === "free"}
        turns={turns}
        busy={busy}
        trace={trace}
        onOpenTrace={(runId) => void openTrace(runId)}
        onCloseTrace={() => setTrace(EMPTY_TRACE)}
      />
      </div>
      {view.kind !== "home" && (historyOpen ? (
        <ConversationHistoryPanel
          conversations={conversations}
          currentConversationId={conversationId}
          loading={historyLoading}
          pinnedIds={pinnedIds}
          agentNames={agentNames}
          onSelect={(id) => void restoreConversation(id)}
          onDelete={(id) => void handleDeleteConversation(id)}
          onTogglePin={togglePin}
          onNewConversation={() => {
            setTurns([]);
            setConversationId(undefined);
            void loadConversations();
          }}
          onCollapse={() => setHistoryOpen(false)}
        />
      ) : (
        <HistoryToggleButton onClick={() => setHistoryOpen(true)} />
      ))}
    </div>
  );
}
