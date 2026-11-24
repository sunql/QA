/** I3 采购 Copilot 页（设计稿 §8.4 的统一对话入口 / M4-2 前端面）。
 *
 * 产品口径（与后端一致，2026-11-21）：
 * - **场景**来自 `GET /procurement/agents/catalog`（只列 runnable）；
 * - **模型**必须显式选择（后端必填 `modelConfigId`，且**不会**替你改派）；
 * - **单轮**：每条消息 = 一次独立只读运行，没有会话记忆；历史只做展示；
 * - **不展示 handlerRef**：轨迹只给步骤/工具名/计划/答案，与目录同一口径。
 *
 * 暂停（PAUSED）不是失败：多候选/缺参数时 Agent 会 ASK_USER，
 * 页面把它渲染成「需要你补充」的追问，用户接着发下一条即可。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  DatePicker,
  Drawer,
  Empty,
  Input,
  InputNumber,
  message,
  Modal,
  Row,
  Segmented,
  Select,
  Statistic,
  Space,
  Spin,
  Tabs,
  Timeline,
  Tooltip,
  Tag,
  Typography,
} from "antd";
import {
  ArrowRightOutlined,
  TableOutlined,
  CarOutlined,
  CommentOutlined,
  LeftOutlined,
} from "@ant-design/icons";
import { useTranslation } from "../i18n";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "../stores/authStore";
import ConversationHistoryPanel from "../components/copilot/ConversationHistoryPanel";
import PendingDeliveryResultCard, {
  parsePendingDeliveryStep,
} from "../components/copilot/PendingDeliveryResultCard";
import HistoryToggleButton from "../components/chat/HistoryToggleButton";
import {
  getAgentCatalog,
  type AgentCatalogEntry,
} from "../api/procurementKnowledge";
import { listModels } from "../api/modelConfig";
import { listDataSources } from "../api/datasource";
import { searchMappings } from "../api/entityMapping";
import {
  listPendingDeliverySites,
  type PendingDeliveryQueryFilters,
} from "../api/procurementCopilot";
import type { Dayjs } from "dayjs";

const { RangePicker } = DatePicker;
import type { DataSource } from "../types/datasource";
import type { ModelConfig } from "../types/modelConfig";
import { getAgentRunSteps } from "../api/agentOrchestrationApi";
import type { AgentRunStepRead } from "../types/agentOrchestration";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  deleteCopilotConversation,
  getCopilotConversation,
  getPendingDeliverySummary,
  listCopilotConversations,
  streamCopilotMessage,
  type PendingDeliverySummaryResponse,
  type CopilotConversationSummary,
  type CopilotDonePayload,
  type CopilotStepPayload,
  type CopilotTurnRead,
} from "../api/procurementCopilot";

const { TextArea } = Input;
const { Text } = Typography;

/** 运行状态 → 界面轮次状态（PAUSED 是正常终态，渲染成 DONE + pauseCode）。 */
const RESTORED_STATUS: Record<string, CopilotTurn["status"]> = {
  SUCCEEDED: "DONE",
  PAUSED: "DONE",
  FAILED: "ERROR",
  CANCELLED: "ERROR",
};

interface CopilotTurn {
  id: string;
  question: string;
  /** 创建时间（ms）——同条件结果复用的 24h 窗口依据 */
  createdAt?: number;
  agentCode: string;
  /** true = 对上一轮追问的回答（后端 resume 同一 run） */
  resumed?: boolean;
  runId: string | null;
  steps: CopilotStepPayload[];
  status: "RUNNING" | "DONE" | "ERROR";
  answer: string | null;
  pauseCode: string | null;
  error: string | null;
  totalTokens: number;
}

const STATUS_COLOR: Record<string, string> = {
  RUNNING: "processing",
  DONE: "success",
  ERROR: "error",
};

/** 执行轨迹抽屉状态（`runId` 用作并发守卫：慢响应回来时不覆盖新打开的轨迹）。 */
interface TraceState {
  runId: string | null;
  loading: boolean;
  error: string | null;
  steps: AgentRunStepRead[];
}

const EMPTY_TRACE: TraceState = {
  runId: null,
  loading: false,
  error: null,
  steps: [],
};

function askOf(turn: CopilotTurn): string | null {
  const ask = [...turn.steps].reverse().find((step) => step.action === "ASK_USER");
  return ask?.planSummary ?? null;
}

/** 后端恢复的轮次 → 页面轮次（无流式步骤，轨迹按 runId 现拉）。 */
/** REST 终态步骤 → 流式步骤形态：关键差异是 outputJson（observation 的
 * {text, truncated}）与**终态 status** —— 流式只推步骤到场一次（RUNNING），
 * 完成态必须回读才能渲染结构化结果卡（2026-11-23 生产实证）。 */
/** 置顶会话的本地留存键。 */
const PINNED_STORAGE_KEY = "copilot.pinnedConversations";

/** 消息 → 短标题：截到参数括号「（」前（即用户的主句），兜底 24 字。 */
function shortTitle(question: string): string {
  const cut = question.indexOf("（");
  const base = cut > 0 ? question.slice(0, cut) : question;
  return base.length > 24 ? `${base.slice(0, 24)}…` : base;
}

function fmtOpenQty(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(value);
}

function restoredStep(step: AgentRunStepRead): CopilotStepPayload {
  return {
    stepNo: step.stepNo,
    iterationNo: step.iterationNo,
    kind: step.kind,
    status: step.status,
    action: step.action ?? null,
    planSummary: step.planSummary ?? null,
    toolName: step.toolName ?? null,
    answer: step.answer ?? null,
    errorCode: step.errorCode ?? null,
    errorMessage: step.errorMessage ?? null,
    output: (step.outputJson as { text?: string } | null) ?? null,
  };
}

function restoredTurn(row: CopilotTurnRead, agentCode: string): CopilotTurn {
  return {
    id: `turn-${row.seq}-${row.runId}`,
    question: row.question,
    agentCode,
    resumed: false,
    runId: row.runId,
    steps: [],
    status: RESTORED_STATUS[row.status] ?? "RUNNING",
    answer: row.answer,
    pauseCode: row.pauseCode,
    error: null,
    totalTokens: 0,
  };
}

export default function ProcurementCopilotPage() {
  const { t } = useTranslation();
  const nav = useNavigate();
  // 数据源切换是治理动作（可指向别的业务库）⇒ 仅 admin 可见（2026-11-24 需求）
  const user = useAuthStore((s) => s.user);
  const isAdmin = (user?.roles ?? []).includes("admin");
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
  /** 置顶的会话（本地持久化；跨设备同步需上移后端 ⇒ 加迁移，另批）。 */
  const [pinnedIds, setPinnedIds] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem(PINNED_STORAGE_KEY);
      const parsed = raw ? (JSON.parse(raw) as unknown) : [];
      return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === "string") : [];
    } catch {
      return [];
    }
  });
  const togglePin = useCallback((conversationId: string) => {
    setPinnedIds((prev) => {
      const next = prev.includes(conversationId)
        ? prev.filter((id) => id !== conversationId)
        : [conversationId, ...prev];
      try {
        localStorage.setItem(PINNED_STORAGE_KEY, JSON.stringify(next));
      } catch {
        /* 隐私模式/配额满：仅内存生效 */
      }
      return next;
    });
  }, []);
  // 历史侧栏默认收起（2026-11-24 反馈）：聚焦当前对话，历史从右侧浮钮展开
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(true);
  const abortRef = useRef<AbortController | null>(null);

  const loadOptions = useCallback(async () => {
    setLoadError(null);
    try {
      const [catalog, modelRows, dsRows] = await Promise.all([
        getAgentCatalog(),
        listModels(true),
        listDataSources(true),
      ]);
      const runnable = catalog.filter((entry) => entry.runnable);
      setScenarios(runnable);
      setModels(modelRows);
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
  // （自动恢复会串账号：storage 按浏览器存，换账号后 detail 404；
  //   且上个会话若是待到货，看起来像「默认进到待到货」。2026-11-24 生产实证。）
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
   * 打开执行轨迹。
   *
   * 必须**带 Authorization 拉取**再在页内渲染：`/agent-runs/{runId}/steps` 要登录，
   * 而新窗口/新标签的裸导航带不上 Bearer 头 ⇒ 只会看到 `{"error":"请先登录"}`
   * （2026-11-23 生产实证）。
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

  /** 页面两级结构：home = 场景卡片墙；进入场景后才是槽位 + 对话。 */
  const [view, setView] = useState<"home" | "pending_delivery" | "free">("home");

  /** 待到货场景槽位（P0 场景；工具 pending_delivery_query，探针定口径）。
   * 场景钉死 Agent：pending_delivery_query 只绑在 PROCUREMENT_SOURCING 上，
   * 用别的 Agent 跑只会得到「工具不存在」的自我描述（2026-11-23 生产实证）。 */
  const PENDING_DELIVERY_AGENT = "PROCUREMENT_SOURCING";
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
    return map;
  }, [scenarios]);

  /** 会话内带结构化结果的轮次（顺序）——多结果用 Tabs 切换，不再纵向堆叠文字墙。 */
  const resultTurns = turns
    .map((turn) => ({
      turn,
      parsed: turn.steps
        .map((step) => parsePendingDeliveryStep(step))
        .find((parsed) => parsed !== null) ?? null,
    }))
    .filter((item) => item.parsed !== null);

  /** 推荐问题 = 预设槽位组合（chips 承载任务模板，槽位表单降级为高级入口）。 */
  const PENDING_DELIVERY_CHIPS = [
    { label: "全部供应商概览（近 12 个月）", monthsBack: 12, overdue: 0 },
    { label: "近期欠交（近 3 个月）", monthsBack: 3, overdue: 0 },
    { label: "逾期超 30 天的欠交", monthsBack: 12, overdue: 30 },
    { label: "长期逾期（超 90 天）", monthsBack: 12, overdue: 90 },
  ];
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [datasourceId, setDatasourceId] = useState<number | undefined>();
  const [monthsBack, setMonthsBack] = useState(12);
  const [supplierKeys, setSupplierKeys] = useState<string[]>([]);
  const [materialKeys, setMaterialKeys] = useState<string[]>([]);
  const [siteKeys, setSiteKeys] = useState<string[]>([]);
  const [dateMode, setDateMode] = useState<"months" | "range">("months");
  const [dateRange, setDateRange] = useState<[Dayjs, Dayjs] | null>(null);
  const [minOverdueDays, setMinOverdueDays] = useState(0);
  const [siteOptions, setSiteOptions] = useState<{ code: string; name: string }[]>([]);
  /** 高级参数默认值；进入场景（卡片墙 → 工作区）时整体重置，跨会话不串扰。 */
  const resetAdvancedParams = useCallback(() => {
    setMonthsBack(12);
    setSupplierKeys([]);
    setMaterialKeys([]);
    setSiteKeys([]);
    setDateMode("months");
    setDateRange(null);
    setMinOverdueDays(0);
    setShowAdvanced(false);
  }, []);

  /** 是否改过高级参数（改了 ⇒ 不写字也能发送）。 */
  const hasAdvancedFilters =
    supplierKeys.length > 0 ||
    materialKeys.length > 0 ||
    siteKeys.length > 0 ||
    minOverdueDays > 0 ||
    monthsBack !== 12 ||
    (dateMode === "range" && dateRange !== null);


  const [supplierOptions, setSupplierOptions] = useState<
    { code: string; name: string }[]
  >([]);
  const [materialOptions, setMaterialOptions] = useState<
    { code: string; name: string }[]
  >([]);
  /** 远程搜索防抖（Select onSearch 高频触发）。 */
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const remoteSearch = useCallback(
    (entityType: "SUPPLIER" | "MATERIAL", q: string, setter: (rows: { code: string; name: string }[]) => void) => {
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
      searchTimerRef.current = setTimeout(() => {
        void searchMappings(q, { entityType, limit: 20 })
          .then((rows) =>
            setter(
              rows.map((row) => ({
                code: row.enterpriseCode,
                name: row.name ?? row.enterpriseCode,
              })),
            ),
          )
          .catch(() => undefined);
      }, 300);
    },
    [],
  );

  /** 基地选项：进场景/换数据源拉一次。 */
  useEffect(() => {
    if (view !== "pending_delivery") return;
    listPendingDeliverySites(datasourceId)
      .then((rows) => setSiteOptions(Array.isArray(rows) ? rows : []))
      .catch(() => setSiteOptions([]));
  }, [view, datasourceId]);

  const effectiveDates = useCallback((): { dateFrom?: string; dateTo?: string } => {
    if (dateMode === "range" && dateRange) {
      return { dateFrom: dateRange[0].format("YYYY-MM-DD"), dateTo: dateRange[1].format("YYYY-MM-DD") };
    }
    return {};
  }, [dateMode, dateRange]);

  const currentFilters = useCallback((): PendingDeliveryQueryFilters => ({
    supplierKeys: supplierKeys,
    materialKeys: materialKeys,
    siteKeys: siteKeys,
    ...effectiveDates(),
  }), [supplierKeys, materialKeys, siteKeys, effectiveDates]);

  /** 欢迎态业务现状：进入场景/换数据源/换时间窗即拉取（零 LLM 成本）。 */
  useEffect(() => {
    if (view !== "pending_delivery") return;
    let cancelled = false;
    setSummaryState((prev) => ({ ...prev, loading: true, error: null }));
    getPendingDeliverySummary({
      datasourceId,
      monthsBack,
      ...currentFilters(),
    })
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
  }, [view, datasourceId, monthsBack, currentFilters]);

  const composePendingDeliveryQuestion = useCallback(
    (overrides?: {
      monthsBack?: number;
      supplierKeys?: string[];
      materialKeys?: string[];
      siteKeys?: string[];
      dateFrom?: string;
      dateTo?: string;
      overdue?: number;
      questionText?: string;
    }) => {
      // chips 传显式槽位：setXxx 是异步 state，闭包里取到的还是旧值
      // （2026-11-24 实测：chip「近 3 个月」会发成 12 个月）
      const months = overrides?.monthsBack ?? monthsBack;
      const suppliers = overrides?.supplierKeys ?? supplierKeys;
      const materials = overrides?.materialKeys ?? materialKeys;
      const sites = overrides?.siteKeys ?? siteKeys;
      const overdue = overrides?.overdue ?? minOverdueDays;
      const ds = datasources.find((d) => d.id === datasourceId);
      const dsText = ds ? `数据源 id=${ds.id}（${ds.name}）` : "数据源=默认";

      // 用户在输入栏写的文字作为主句；**冲突的槽位以对话框为准**——
      // 文字里提到了哪个维度，高级参数里对应维度就让位（2026-11-24 生产反馈）。
      // 两层启发式（规划器是最终仲裁，尾注里同时附带「冲突以文字为准」指令）：
      //   ① 关键词：供应商/物料/基地/时间窗…
      //   ② 编码模式：B109/F109（供应商码）、AJE0000781/长数字（物料码）、T3/C1（基地码）
      //     ——「分析一下B109待到货情况」没写「供应商」三个字也算指定（生产实测漏判）
      const lead = overrides?.questionText?.trim() ?? "";
      const mentionsSupplier = /供应商|厂家/.test(lead) || /\b[A-Z]\d{2,4}\b/.test(lead);
      const mentionsMaterial =
        /物料|材料/.test(lead) || /\b[A-Z]{2,}\d{4,}\b/.test(lead) || /\b\d{10,}\b/.test(lead);
      const mentionsSite = /基地|厂区|工厂|车间/.test(lead) || /\bT\d{1,2}\b|\bC\d\b/.test(lead);
      const mentionsTime = /时间窗|个月|时间段|日期|近\d|上[半]年|本年度|本年/.test(lead);

      const parts: string[] = [dsText];
      if (!mentionsTime) {
        parts.push(
          overrides?.dateFrom && overrides?.dateTo
            ? `时间窗=${overrides.dateFrom}~${overrides.dateTo}`
            : `时间窗=近${months}个月`,
        );
      }
      if (!mentionsSupplier) {
        parts.push(`供应商=${suppliers.length > 0 ? suppliers.join("、") : "全部"}`);
      }
      if (!mentionsMaterial && materials.length > 0) {
        parts.push(`物料=${materials.join("、")}`);
      }
      if (!mentionsSite && sites.length > 0) {
        parts.push(`基地=${sites.join("、")}`);
      }
      parts.push(`最低逾期天数=${overdue}`);

      // 工具入参：仅传**未让位**的非空维度（格式收敛，修复此前 `[；` 毛刺）
      const toolArgs: string[] = [];
      if (!mentionsSupplier && suppliers.length > 0) {
        toolArgs.push(`supplierKeys=[${suppliers.join(", ")}]`);
      }
      if (!mentionsMaterial && materials.length > 0) {
        toolArgs.push(`materialKeys=[${materials.join(", ")}]`);
      }
      if (!mentionsSite && sites.length > 0) {
        toolArgs.push(`siteKeys=[${sites.join(", ")}]`);
      }
      if (!mentionsTime && overrides?.dateFrom && overrides?.dateTo) {
        toolArgs.push(`dateFrom=${overrides.dateFrom}；dateTo=${overrides.dateTo}`);
      }
      const argsText = toolArgs.length > 0 ? `；${toolArgs.join("；")}` : "";

      return (
        `${lead || "分析待到货情况"}（${parts.join("；")}）。` +
        `请调用 pending_delivery_query 工具（datasourceId=${datasourceId ?? "缺省"}${argsText}），` +
        "按供应商汇总欠交量并给催货建议，引用订单号与物料编码。" +
        "若上方括号内的参数与用户问题描述冲突，以用户问题描述为准。"
      );
    },
    [monthsBack, supplierKeys, materialKeys, siteKeys, minOverdueDays, datasources, datasourceId],
  );

  const handleSend = async (
    textOverride?: string,
    options?: { agentCode?: string },
  ) => {
    const text = (textOverride ?? question).trim();
    // 场景会钉死自己的 Agent：与当前会话绑定不一致 ⇒ 后端 422（会话绑定 Agent），
    // 所以这里换 Agent 时必须开新会话（清 conversationId）。
    const scenarioAgent = options?.agentCode;
    const effectiveAgent = scenarioAgent ?? agentCode;
    const effectiveConversation =
      scenarioAgent && scenarioAgent !== agentCode ? undefined : conversationId;
    if (!text || !effectiveAgent || !modelId || busy) return;
    if (scenarioAgent && scenarioAgent !== agentCode) {
      setAgentCode(scenarioAgent);
      setConversationId(undefined);
    }
    const id = `turn-${Date.now()}`;
    setTurns((prev) => [
      ...prev,
      {
        id,
        question: text,
        createdAt: Date.now(),
        agentCode: effectiveAgent,
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
          message: text,
          agentCode: effectiveAgent,
          modelConfigId: modelId,
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
        const message =
          err instanceof Error ? err.message : t("procurement.copilot.sendFailed");
        // 只覆盖「还在跑」的轮次：若已收到终态事件（done / timeout 等），
        // 之后 nginx 重置连接导致的 TypeError 不应把有意义的文案冲掉
        // （2026-11-23：ERR_INCOMPLETE_CHUNKED_ENCODING 覆盖了超时提示）。
        setTurns((prev) =>
          prev.map((turn) =>
            turn.id === id && turn.status === "RUNNING"
              ? { ...turn, status: "ERROR", error: message }
              : turn,
          ),
        );
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  };

  return (
    <div style={{ display: "flex", height: "100%", position: "relative" }}>
      <div style={{ flex: 1, minWidth: 0, padding: 24, overflowY: "auto" }}>
      <Space align="center" style={{ marginBottom: 12 }}>
        {view !== "home" && (
          <Button
            icon={<LeftOutlined />}
            onClick={() => setView("home")}
          >
            {t("procurement.copilot.backToScenarios")}
          </Button>
        )}
        <h2 style={{ margin: 0 }}>
          {view === "home"
            ? t("procurement.copilot.title")
            : `${t("procurement.copilot.title")} · ${
                view === "pending_delivery"
                  ? t("procurement.copilot.scenarioDelivery")
                  : t("procurement.copilot.scenarioFree")
              }`}
        </h2>
      </Space>
      {view === "home" ? (
        <Row gutter={[16, 16]}>
          <Col xs={24} md={12} lg={8}>
            <Card
              hoverable
              style={{ height: "100%", borderRadius: 12 }}
              styles={{ body: { padding: 16, display: "flex", flexDirection: "column", gap: 10 } }}
              onClick={() => {
                // 进入场景 = 全新工作区（参考系统的「进入」语义）：
                // 旧对话留在历史侧栏，不清场则欢迎态永远被旧轮次顶掉；
                // 高级参数同样重置 —— 不同会话/不同 chip 之间不串扰
                // （同会话连续追问不受影响：只有从卡片墙进入才重置）
                setTurns([]);
                setConversationId(undefined);
                resetAdvancedParams();
                setActiveResultTurnId(null);
                setView("pending_delivery");
              }}
            >
              <div
                style={{
                  height: 110, borderRadius: 8,
                  background: "linear-gradient(135deg, #e6f4ff 0%, #f0f7ff 100%)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                }}
              >
                <CarOutlined style={{ fontSize: 48, color: "#1677ff" }} />
              </div>
              <Text strong style={{ fontSize: 16 }}>
                {t("procurement.copilot.scenarioDelivery")}
              </Text>
              <Text type="secondary" style={{ minHeight: 44 }}>
                {t("procurement.copilot.scenarioDeliveryDesc")}
              </Text>
              <Button type="primary" block icon={<ArrowRightOutlined />} iconPosition="end">
                {t("procurement.copilot.enterScenario")}
              </Button>
            </Card>
          </Col>
          <Col xs={24} md={12} lg={8}>
            <Card
              hoverable
              style={{ height: "100%", borderRadius: 12 }}
              styles={{ body: { padding: 16, display: "flex", flexDirection: "column", gap: 10 } }}
              onClick={() => setView("free")}
            >
              <div
                style={{
                  height: 110, borderRadius: 8,
                  background: "linear-gradient(135deg, #f0f5ff 0%, #f9f0ff 100%)",
                  display: "flex", alignItems: "center", justifyContent: "center",
                }}
              >
                <CommentOutlined style={{ fontSize: 48, color: "#2f54eb" }} />
              </div>
              <Text strong style={{ fontSize: 16 }}>
                {t("procurement.copilot.scenarioFree")}
              </Text>
              <Text type="secondary" style={{ minHeight: 44 }}>
                {t("procurement.copilot.scenarioFreeDesc")}
              </Text>
              <Button type="primary" block icon={<ArrowRightOutlined />} iconPosition="end">
                {t("procurement.copilot.enterScenario")}
              </Button>
            </Card>
          </Col>
        </Row>
      ) : (
        <>
      {view === "pending_delivery" && turns.length === 0 && (
        <>
          <Text
            type="secondary"
            style={{ letterSpacing: 2, fontSize: 12, display: "block" }}
          >
            PENDING DELIVERY
          </Text>
          <Typography.Title level={3} style={{ marginTop: 4, marginBottom: 4 }}>
            {t("procurement.copilot.heroGreeting")}
          </Typography.Title>
          <Text type="secondary">{t("procurement.copilot.heroSubtitle")}</Text>
          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} md={16}>
              <Card
                size="small"
                loading={summaryState.loading}
                title={t("procurement.copilot.businessNow")}
              >
                {summaryState.error ? (
                  <Alert type="warning" showIcon message={summaryState.error} />
                ) : summaryState.data ? (
                  <>
                    <Row gutter={12}>
                      <Col span={6}>
                        <Statistic title={t("procurement.copilot.statOpenLines")} value={summaryState.data.summary.openLines} />
                      </Col>
                      <Col span={6}>
                        <Statistic title={t("procurement.copilot.statOpenQty")} value={summaryState.data.summary.openQty} precision={2} />
                      </Col>
                      <Col span={6}>
                        <Statistic title={t("procurement.copilot.statOverdue")} value={summaryState.data.summary.overdueLines} valueStyle={summaryState.data.summary.overdueLines > 0 ? { color: "#cf1322" } : undefined} />
                      </Col>
                      <Col span={6}>
                        <Statistic title={t("procurement.copilot.statExcluded")} value={summaryState.data.summary.excludedRows} />
                      </Col>
                    </Row>
                    {summaryState.data.bySupplier.length > 0 && (
                      <Text type="secondary" style={{ display: "block", marginTop: 8 }}>
                        {t("procurement.copilot.statTopSuppliers")}:{" "}
                        {summaryState.data.bySupplier
                          .slice(0, 3)
                          .map((item) => `${item.supplierCode}(${fmtOpenQty(item.openQty)})`)
                          .join("、")}
                      </Text>
                    )}
                  </>
                ) : null}
              </Card>
            </Col>
            <Col xs={24} md={8}>
              <Card size="small" title={t("procurement.copilot.tryAsk")}>
                          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            {PENDING_DELIVERY_CHIPS.map((chip) => (
              <Button
                key={chip.label}
                block
                style={{ textAlign: "left", display: "flex", justifyContent: "space-between", alignItems: "center" }}
                disabled={busy || !modelId}
                onClick={() => {
                  setMonthsBack(chip.monthsBack);
                  setMinOverdueDays(chip.overdue);
                  void handleSend(
                    composePendingDeliveryQuestion({
                      monthsBack: chip.monthsBack,
                      supplierKeys: [],
                      overdue: chip.overdue,
                    }),
                    { agentCode: PENDING_DELIVERY_AGENT },
                  );
                }}
              >
                <span>{chip.label}</span>
                <ArrowRightOutlined />
              </Button>
            ))}
          </Space>
              </Card>
            </Col>
          </Row>
        </>
      )}
      {view === "pending_delivery" && (
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
                (question.trim() || hasAdvancedFilters)
              ) {
                void handleSend(
                  composePendingDeliveryQuestion({ questionText: question }),
                  { agentCode: PENDING_DELIVERY_AGENT },
                );
              }
            }}
          />
          <Space style={{ marginTop: 8 }}>
            <Button
              type="primary"
              loading={busy}
              disabled={busy || !modelId || (!question.trim() && !hasAdvancedFilters)}
              onClick={() =>
                void handleSend(
                  composePendingDeliveryQuestion({ questionText: question }),
                  { agentCode: PENDING_DELIVERY_AGENT },
                )
              }
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
      {view === "pending_delivery" && showAdvanced && (
        <Card
          size="small"
          style={{
            marginBottom: 16,
            border: "1.5px solid #91caff",
            background: "#f7faff",
            borderRadius: 8,
          }}
          title={
            <Text strong style={{ color: "#1677ff" }}>
              {t("procurement.copilot.advancedParams")}
            </Text>
          }
        >
          <Space wrap size={[16, 8]} align="center">
            <div style={{ fontSize: 12, color: "#5a6b87", marginBottom: 4 }}>{t("procurement.copilot.model")}</div>
            <Select
              style={{ minWidth: 160 }}
              placeholder={t("procurement.copilot.model")}
              value={modelId}
              onChange={setModelId}
              options={models.map((model) => ({
                value: model.id,
                label: model.modelName,
              }))}
            />
            {/* 数据源切换 = 治理动作（可指向别的业务库）⇒ 仅 admin 可见 */}
            {isAdmin && (
              <>
                <div style={{ fontSize: 12, color: "#5a6b87", marginBottom: 4 }}>
                  {t("procurement.copilot.slotDatasource")}
                </div>
                <Select
                  style={{ minWidth: 150 }}
                  placeholder={t("procurement.copilot.slotDatasource")}
                  value={datasourceId}
                  onChange={setDatasourceId}
                  options={datasources.map((d) => ({
                    value: d.id,
                    label: d.name,
                  }))}
                />
              </>
            )}
            <div style={{ fontSize: 12, color: "#5a6b87", marginBottom: 4 }}>{t("procurement.copilot.slotTime")}</div>
            <Segmented
              value={dateMode}
              onChange={(value) => setDateMode(value as "months" | "range")}
              options={[
                { label: t("procurement.copilot.timeMonths"), value: "months" },
                { label: t("procurement.copilot.timeRange"), value: "range" },
              ]}
            />
            {dateMode === "months" ? (
              <>
                <InputNumber
                  min={1}
                  max={36}
                  value={monthsBack}
                  onChange={(v) => setMonthsBack(Number(v) || 12)}
                />
                <span style={{ fontSize: 12, color: "#5a6b87" }}>{t("procurement.copilot.timeMonthsUnit")}</span>
              </>
            ) : (
              <RangePicker
                size="middle"
                value={dateRange}
                onChange={(value) => {
                  if (value && value[0] && value[1]) {
                    setDateRange([value[0], value[1]]);
                  } else {
                    setDateRange(null);
                  }
                }}
              />
            )}
            <div style={{ fontSize: 12, color: "#5a6b87", marginBottom: 4 }}>{t("procurement.copilot.slotSupplier")}</div>
            <Select
              mode="multiple"
              showSearch
              allowClear
              maxTagCount={1}
              style={{ minWidth: 200, maxWidth: 320 }}
              placeholder={t("procurement.copilot.searchSupplier")}
              value={supplierKeys}
              onSearch={(q) => remoteSearch("SUPPLIER", q, setSupplierOptions)}
              onChange={setSupplierKeys}
              options={supplierOptions.map((item) => ({
                value: item.code,
                label: `${item.name}（${item.code}）`,
              }))}
              filterOption={false}
            />
            <div style={{ fontSize: 12, color: "#5a6b87", marginBottom: 4 }}>{t("procurement.copilot.slotMaterial")}</div>
            <Select
              mode="multiple"
              showSearch
              allowClear
              maxTagCount={1}
              style={{ minWidth: 200, maxWidth: 320 }}
              placeholder={t("procurement.copilot.searchMaterial")}
              value={materialKeys}
              onSearch={(q) => remoteSearch("MATERIAL", q, setMaterialOptions)}
              onChange={setMaterialKeys}
              options={materialOptions.map((item) => ({
                value: item.code,
                label: `${item.name}（${item.code}）`,
              }))}
              filterOption={false}
            />
            <div style={{ fontSize: 12, color: "#5a6b87", marginBottom: 4 }}>{t("procurement.copilot.slotSite")}</div>
            <Select
              mode="multiple"
              showSearch
              allowClear
              maxTagCount={2}
              style={{ minWidth: 220, maxWidth: 340 }}
              placeholder={t("procurement.copilot.searchSite")}
              value={siteKeys}
              onChange={setSiteKeys}
              optionFilterProp="label"
              options={siteOptions.map((item) => ({
                value: item.code,
                label: `${item.name}（${item.code}）`,
              }))}
            />
            <div style={{ fontSize: 12, color: "#5a6b87", marginBottom: 4 }}>{t("procurement.copilot.slotOverdue")}</div>
            <InputNumber
              min={0}
              value={minOverdueDays}
              onChange={(v) => setMinOverdueDays(Number(v) || 0)}
            />
          </Space>
        </Card>
      )}
      {view === "free" && (
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={t("procurement.copilot.singleTurnHint")}
      />
      )}
      {view === "free" && (
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
              if (event.ctrlKey || event.metaKey) void handleSend();
            }}
          />
          <Space style={{ marginTop: 8 }}>
            <Button
              type="primary"
              loading={busy}
              disabled={!question.trim() || !agentCode || !modelId}
              onClick={() => void handleSend()}
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
      {view === "pending_delivery" && resultTurns.length > 0 && (() => {
        const activeIndex = resultTurns.findIndex(
          (item) => item.turn.id === (activeResultTurnId ?? resultTurns[resultTurns.length - 1].turn.id),
        );
        const active = resultTurns[activeIndex >= 0 ? activeIndex : resultTurns.length - 1];
        return (
          <Card
            size="small"
            styles={{ body: { padding: "8px 16px 16px" } }}
            title={
              <Space size={8}>
                <Text strong style={{ fontSize: 14 }}>
                  {t("procurement.copilot.resultsTitle")}
                </Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {t("procurement.copilot.resultsHint")}
                </Text>
              </Space>
            }
            style={{ marginBottom: 16 }}
          >
            <Tabs
              type="card"
              size="middle"
              activeKey={active.turn.id}
              onChange={setActiveResultTurnId}
              items={resultTurns.map((item, index) => ({
                key: item.turn.id,
                label: (
                  <Tooltip title={item.turn.question} placement="top">
                    <span style={{ fontSize: 13 }}>
                      {`#${index + 1} ${item.turn.question.slice(0, 14)}${
                        item.turn.question.length > 14 ? "…" : ""
                      }`}
                    </span>
                  </Tooltip>
                ),
              }))}
            />
            {active.parsed ? (
              <>
                <PendingDeliveryResultCard result={active.parsed} />
                <Button
                  size="small"
                  icon={<TableOutlined />}
                  onClick={() => void nav("/procurement/pending-delivery")}
                >
                  {t("procurement.copilot.viewPendingDeliveryLines")}
                </Button>
              </>
            ) : (
              <Empty description={t("procurement.copilot.traceEmpty")} />
            )}
          </Card>
        );
      })()}
      {view !== "home" && (
      <Spin spinning={busy && turns.length === 0}>
        {turns.length === 0 ? (
          view === "free" ? (
            <Empty description={t("procurement.copilot.empty")} />
          ) : null
        ) : (
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            {[...turns].reverse().map((turn) => (
              <div key={turn.id}>
                {/* 失败轮次的错误摘要外置（抽屉默认收起，不能把错误藏起来） */}
                {turn.status === "ERROR" && turn.error && (
                  <Alert
                    type="error"
                    showIcon
                    style={{ marginBottom: 8 }}
                    message={
                      <Space wrap>
                        <Text strong>{turn.question}</Text>
                        <Text>{turn.error}</Text>
                      </Space>
                    }
                  />
                )}
                <Collapse
                  items={[{
                    key: turn.id,
                    forceRender: true,
                    label: (
                      <Space wrap>
                        <Tag color={STATUS_COLOR[turn.status]}>
                          {turn.status === "DONE"
                            ? t("procurement.copilot.contentTag")
                            : turn.status}
                        </Tag>
                        <Text strong>{shortTitle(turn.question)}</Text>
                        <Text type="secondary">{turn.agentCode}</Text>
                        {turn.resumed && (
                          <Tag color="blue">{t("procurement.copilot.resumed")}</Tag>
                        )}
                        {turn.pauseCode && <Tag color="warning">{turn.pauseCode}</Tag>}
                      </Space>
                    ),
                    children: (
                      <>
                        {/* 完整消息（含参数尾注）默认不可见，展开抽屉在此查看 */}
                        <div
                          style={{
                            marginBottom: 8,
                            padding: "6px 10px",
                            background: "#fafafa",
                            border: "1px solid #f0f0f0",
                            borderRadius: 6,
                          }}
                        >
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {t("procurement.copilot.detailContent")}：
                          </Text>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {turn.question}
                          </Text>
                        </div>
                        {turn.answer && (
                          <Card
                            size="small"
                            title={t("procurement.copilot.answerTitle")}
                            style={{ marginBottom: 8 }}
                          >
                            <div className="markdown-body">
                              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                {turn.answer}
                              </ReactMarkdown>
                            </div>
                          </Card>
                        )}
                        {askOf(turn) && (
                          <Alert
                            type="warning"
                            showIcon
                            style={{ marginBottom: 8 }}
                            message={askOf(turn)}
                          />
                        )}
                        <Space wrap>
                          {turn.runId && (
                            <Button
                              size="small"
                              onClick={() => {
                                if (turn.runId) void openTrace(turn.runId);
                              }}
                            >
                              {t("procurement.copilot.openTrace")}
                            </Button>
                          )}
                          {turn.totalTokens > 0 && (
                            <Text type="secondary">
                              {t("procurement.copilot.tokens")}: {turn.totalTokens}
                            </Text>
                          )}
                        </Space>
                      </>
                    ),
                  }]}
                />
              </div>
            ))}
          </Space>
        )}
      </Spin>
      )}
      {/* ④ 分析进度弹窗：发送后出结果前可见，终态自动关闭 */}
      <Modal
        open={busy}
        footer={null}
        closable={false}
        maskClosable={false}
        keyboard={false}
        width={360}
        centered
      >
        <div style={{ textAlign: "center", padding: "16px 0" }}>
          <Spin size="large" />
          <div style={{ marginTop: 12 }}>
            <Text strong>{t("procurement.copilot.analyzing")}</Text>
          </div>
          <Text type="secondary" style={{ display: "block", marginTop: 4 }}>
            {turns[turns.length - 1]?.question?.slice(0, 30)}
            {(turns[turns.length - 1]?.question?.length ?? 0) > 30 ? "…" : ""}
          </Text>
          <Text type="secondary" style={{ display: "block", marginTop: 4 }}>
            {t("procurement.copilot.analyzingTip")}
          </Text>
        </div>
      </Modal>
      <Drawer
        title={t("procurement.copilot.traceTitle")}
        width={640}
        open={trace.runId !== null}
        onClose={() => setTrace(EMPTY_TRACE)}
      >
        {trace.loading && <Spin />}
        {!trace.loading && trace.error && (
          <Alert type="error" showIcon message={trace.error} />
        )}
        {!trace.loading && !trace.error && trace.steps.length === 0 && (
          <Empty description={t("procurement.copilot.traceEmpty")} />
        )}
        {!trace.loading && !trace.error && trace.steps.length > 0 && (
          <Timeline
            items={trace.steps.map((step) => {
              const stepColor =
                step.status === "SUCCEEDED" ? "green"
                : step.status === "RUNNING" ? "blue"
                : step.status === "FAILED" ? "red" : "gray";
              const kindLabel =
                step.kind === "PLAN" ? "规划"
                : step.kind === "TOOL" ? "工具执行"
                : step.kind;
              return {
                key: `${step.stepNo}`,
                color: stepColor,
                children: (
                  <div style={{ paddingBottom: 8 }}>
                    <Space wrap size={6} style={{ marginBottom: 4 }}>
                      <Text strong style={{ fontSize: 13 }}>
                        {`#${step.stepNo} ${kindLabel}`}
                      </Text>
                      {step.toolName && <Tag color="geekblue">{step.toolName}</Tag>}
                      <Tag color={stepColor}>{step.status}</Tag>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {t("procurement.copilot.tokens")}:{" "}
                        {step.promptTokens + step.completionTokens}
                      </Text>
                    </Space>
                    {/* 计划/答案正文走 Markdown（分段可读，不再是文字墙） */}
                    {step.planSummary && (
                      <div className="markdown-body" style={{ fontSize: 13 }}>
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {step.planSummary}
                        </ReactMarkdown>
                      </div>
                    )}
                    {step.answer && (
                      <div className="markdown-body" style={{ fontSize: 13 }}>
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {step.answer}
                        </ReactMarkdown>
                      </div>
                    )}
                    {step.errorMessage && (
                      <Text type="danger" style={{ fontSize: 12 }}>
                        {step.errorMessage}
                      </Text>
                    )}
                  </div>
                ),
              };
            })}
          />
        )}
      </Drawer>
      </div>
      {view !== "home" && (historyOpen ? (
        <ConversationHistoryPanel
          conversations={conversations}
          currentConversationId={conversationId}
          loading={historyLoading}
          pinnedIds={pinnedIds}
          agentNames={agentNames}
          onSelect={(id) => void restoreConversation(id)}
          onDelete={(id) => handleDeleteConversation(id)}
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
