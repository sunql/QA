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

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Drawer,
  Empty,
  Input,
  InputNumber,
  List,
  Row,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import {
  ArrowRightOutlined,
  CarOutlined,
  CommentOutlined,
  LeftOutlined,
} from "@ant-design/icons";
import { useTranslation } from "../i18n";
import ConversationHistoryPanel from "../components/copilot/ConversationHistoryPanel";
import HistoryToggleButton from "../components/chat/HistoryToggleButton";
import {
  getAgentCatalog,
  type AgentCatalogEntry,
} from "../api/procurementKnowledge";
import { listModels } from "../api/modelConfig";
import { listDataSources } from "../api/datasource";
import type { DataSource } from "../types/datasource";
import type { ModelConfig } from "../types/modelConfig";
import { getAgentRunSteps } from "../api/agentOrchestrationApi";
import type { AgentRunStepRead } from "../types/agentOrchestration";
import {
  deleteCopilotConversation,
  getCopilotConversation,
  listCopilotConversations,
  streamCopilotMessage,
  type CopilotConversationSummary,
  type CopilotDonePayload,
  type CopilotStepPayload,
  type CopilotTurnRead,
} from "../api/procurementCopilot";

const { TextArea } = Input;
const { Paragraph, Text } = Typography;

/** 当前会话 id 的本地留存键：刷新后自动恢复同一条线程。 */
const CONVERSATION_STORAGE_KEY = "copilot.conversationId";

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
  const [historyOpen, setHistoryOpen] = useState(true);
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
      // 会话不存在/无权访问：清掉本地记录，回到新会话
      localStorage.removeItem(CONVERSATION_STORAGE_KEY);
      setConversationId(undefined);
    }
  }, []);

  // 打开页面：拉历史列表 + 恢复上次的会话（刷新不丢线程）
  useEffect(() => {
    void loadConversations();
    const stored = localStorage.getItem(CONVERSATION_STORAGE_KEY);
    if (stored) {
      setView("free");
      void restoreConversation(stored);
    }
  }, [loadConversations, restoreConversation]);

  /** 删除会话：删当前会话时一并清空线程；随后刷新侧栏。 */
  const handleDeleteConversation = useCallback(
    async (id: string) => {
      await deleteCopilotConversation(id);
      if (id === conversationId) {
        setTurns([]);
        setConversationId(undefined);
      }
      await loadConversations();
    },
    [conversationId, loadConversations],
  );

  // 会话 id 变化即落盘/清理
  useEffect(() => {
    if (conversationId) {
      localStorage.setItem(CONVERSATION_STORAGE_KEY, conversationId);
    } else {
      localStorage.removeItem(CONVERSATION_STORAGE_KEY);
    }
  }, [conversationId]);

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
  const [datasources, setDatasources] = useState<DataSource[]>([]);
  const [datasourceId, setDatasourceId] = useState<number | undefined>();
  const [monthsBack, setMonthsBack] = useState(12);
  const [supplierKey, setSupplierKey] = useState("");
  const [minOverdueDays, setMinOverdueDays] = useState(0);

  const composePendingDeliveryQuestion = useCallback(() => {
    const supplier = supplierKey.trim() || "全部";
    const ds = datasources.find((d) => d.id === datasourceId);
    const dsText = ds ? `数据源 id=${ds.id}（${ds.name}）` : "数据源=默认";
    return (
      `分析待到货情况：${dsText}；时间窗=近${monthsBack}个月；供应商=${supplier}；` +
      `最低逾期天数=${minOverdueDays}。请调用 pending_delivery_query 工具` +
      `（datasourceId=${datasourceId ?? "缺省"}），` +
      "按供应商汇总欠交量并给催货建议，引用订单号与物料编码。"
    );
  }, [monthsBack, supplierKey, minOverdueDays, datasources, datasourceId]);

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
          onDone: (done: CopilotDonePayload) =>
            updateTurn(id, {
              status: "DONE",
              answer: done.answer,
              pauseCode: done.pauseCode,
              totalTokens: done.totalTokens,
            }),
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
        <h2 style={{ margin: 0 }}>{t("procurement.copilot.title")}</h2>
      </Space>
      {view === "home" ? (
        <Row gutter={[16, 16]}>
          <Col xs={24} md={12} lg={8}>
            <Card
              hoverable
              style={{ height: "100%", borderRadius: 12 }}
              styles={{ body: { padding: 16, display: "flex", flexDirection: "column", gap: 10 } }}
              onClick={() => setView("pending_delivery")}
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
      {view === "pending_delivery" && (
        <Card size="small" style={{ marginBottom: 16 }} title={t("procurement.copilot.scenarioDelivery")}>
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Space wrap size={8} align="center">
              <Text type="secondary">{t("procurement.copilot.model")}</Text>
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
            <Space wrap size={4}>
              <Text type="secondary">{t("procurement.copilot.slotDatasource")}</Text>
              <Select
                style={{ minWidth: 200 }}
                placeholder={t("procurement.copilot.slotDatasource")}
                value={datasourceId}
                onChange={setDatasourceId}
                options={datasources.map((d) => ({
                  value: d.id,
                  label: d.name,
                }))}
              />
            </Space>
            <Space wrap size={4}>
              <Text type="secondary">{t("procurement.copilot.slotMonths")}</Text>
              <InputNumber
                min={1}
                max={36}
                value={monthsBack}
                onChange={(v) => setMonthsBack(Number(v) || 12)}
              />
              <Text type="secondary">{t("procurement.copilot.slotOverdue")}</Text>
              <InputNumber
                min={0}
                value={minOverdueDays}
                onChange={(v) => setMinOverdueDays(Number(v) || 0)}
              />
            </Space>
            <Space wrap size={4}>
              <Text type="secondary">{t("procurement.copilot.slotSupplier")}</Text>
              <Input
                style={{ width: 140 }}
                value={supplierKey}
                placeholder={t("procurement.copilot.slotSupplierAll")}
                onChange={(event) => setSupplierKey(event.target.value)}
              />
            </Space>
            <Button
              type="primary"
              disabled={busy || !modelId}
              onClick={() =>
                void handleSend(composePendingDeliveryQuestion(), {
                  agentCode: PENDING_DELIVERY_AGENT,
                })
              }
            >
              {t("procurement.copilot.scenarioRun")}
            </Button>
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
      <Card size="small" style={{ marginBottom: 16 }}>
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
            {conversationId && (
              <Text type="secondary">
                {t("procurement.copilot.conversation")}: {conversationId.slice(0, 8)}…
              </Text>
            )}
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
      {view !== "home" && (
      <Spin spinning={busy && turns.length === 0}>
        {turns.length === 0 ? (
          <Empty description={t("procurement.copilot.empty")} />
        ) : (
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            {[...turns].reverse().map((turn) => (
              <Card
                key={turn.id}
                size="small"
                title={
                  <Space wrap>
                    <Tag color={STATUS_COLOR[turn.status]}>{turn.status}</Tag>
                    <Text strong>{turn.question}</Text>
                    <Text type="secondary">{turn.agentCode}</Text>
                    {turn.resumed && (
                      <Tag color="blue">{t("procurement.copilot.resumed")}</Tag>
                    )}
                    {turn.pauseCode && <Tag color="warning">{turn.pauseCode}</Tag>}
                  </Space>
                }
              >
                {turn.steps.length > 0 && (
                  <ol style={{ paddingLeft: 20, marginTop: 0 }}>
                    {turn.steps.map((step) => (
                      <li key={`${turn.id}-${step.stepNo}`}>
                        <Text type="secondary">
                          {step.kind} · {step.status}
                          {step.toolName ? ` · ${step.toolName}` : ""}
                        </Text>
                        {step.planSummary && <div>{step.planSummary}</div>}
                        {step.errorMessage && (
                          <Text type="danger">{step.errorMessage}</Text>
                        )}
                      </li>
                    ))}
                  </ol>
                )}
                {turn.answer && <Paragraph>{turn.answer}</Paragraph>}
                {askOf(turn) && (
                  <Alert type="warning" showIcon message={askOf(turn)} style={{ marginTop: 8 }} />
                )}
                {turn.error && (
                  <Alert type="error" showIcon message={turn.error} style={{ marginTop: 8 }} />
                )}
                <Space style={{ marginTop: 8 }}>
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
              </Card>
            ))}
          </Space>
        )}
      </Spin>
      )}
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
          <List
            dataSource={trace.steps}
            renderItem={(step) => (
              <List.Item key={step.stepNo}>
                <Space direction="vertical" size={2} style={{ width: "100%" }}>
                  <Space wrap>
                    <Tag>{`#${step.stepNo}`}</Tag>
                    <Text type="secondary">
                      {step.kind} · {step.status}
                      {step.toolName ? ` · ${step.toolName}` : ""}
                    </Text>
                    <Text type="secondary">
                      {t("procurement.copilot.tokens")}:{" "}
                      {step.promptTokens + step.completionTokens}
                    </Text>
                  </Space>
                  {step.planSummary && <div>{step.planSummary}</div>}
                  {step.answer && <div>{step.answer}</div>}
                  {step.errorMessage && (
                    <Text type="danger">{step.errorMessage}</Text>
                  )}
                </Space>
              </List.Item>
            )}
          />
        )}
      </Drawer>
      </div>
      {view !== "home" && (historyOpen ? (
        <ConversationHistoryPanel
          conversations={conversations}
          currentConversationId={conversationId}
          loading={historyLoading}
          onSelect={(id) => void restoreConversation(id)}
          onDelete={(id) => handleDeleteConversation(id)}
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
