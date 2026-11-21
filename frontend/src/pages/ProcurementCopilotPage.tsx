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
  Empty,
  Input,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import { useTranslation } from "../i18n";
import {
  getAgentCatalog,
  type AgentCatalogEntry,
} from "../api/procurementKnowledge";
import { listModels } from "../api/modelConfig";
import type { ModelConfig } from "../types/modelConfig";
import {
  streamCopilotMessage,
  type CopilotDonePayload,
  type CopilotStepPayload,
} from "../api/procurementCopilot";

const { TextArea } = Input;
const { Paragraph, Text } = Typography;

interface CopilotTurn {
  id: string;
  question: string;
  agentCode: string;
  /** true = 对上一轮追问的回答（后端 resume 同一 run） */
  resumed?: boolean;
  runId: string | null;
  stepsUrl: string | null;
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

function askOf(turn: CopilotTurn): string | null {
  const ask = [...turn.steps].reverse().find((step) => step.action === "ASK_USER");
  return ask?.planSummary ?? null;
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
  const abortRef = useRef<AbortController | null>(null);

  const loadOptions = useCallback(async () => {
    setLoadError(null);
    try {
      const [catalog, modelRows] = await Promise.all([
        getAgentCatalog(),
        listModels(true),
      ]);
      const runnable = catalog.filter((entry) => entry.runnable);
      setScenarios(runnable);
      setModels(modelRows);
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

  useEffect(() => () => abortRef.current?.abort(), []);

  const updateTurn = (id: string, patch: Partial<CopilotTurn>) => {
    setTurns((prev) =>
      prev.map((turn) => (turn.id === id ? { ...turn, ...patch } : turn)),
    );
  };

  const handleSend = async () => {
    const text = question.trim();
    if (!text || !agentCode || !modelId || busy) return;
    const id = `turn-${Date.now()}`;
    setTurns((prev) => [
      ...prev,
      {
        id,
        question: text,
        agentCode,
        resumed: undefined,
        runId: null,
        stepsUrl: null,
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
          agentCode,
          modelConfigId: modelId,
          ...(conversationId ? { conversationId } : {}),
        },
        {
          onRun: (run) => {
            if (run.conversationId) setConversationId(run.conversationId);
            updateTurn(id, {
              runId: run.runId,
              stepsUrl: run.stepsUrl,
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
              stepsUrl: done.stepsUrl,
            }),
          onError: (err) =>
            updateTurn(id, { status: "ERROR", error: err.error }),
        },
        { signal: controller.signal, fallbackError: t("procurement.copilot.sendFailed") },
      );
    } catch (err) {
      if (!controller.signal.aborted) {
        updateTurn(id, {
          status: "ERROR",
          error:
            err instanceof Error ? err.message : t("procurement.copilot.sendFailed"),
        });
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
    }
  };

  return (
    <div style={{ padding: 24 }}>
      <h2>{t("procurement.copilot.title")}</h2>
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message={t("procurement.copilot.singleTurnHint")}
      />
      {loadError && (
        <Alert type="error" showIcon message={loadError} style={{ marginBottom: 12 }} />
      )}

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
                  {turn.stepsUrl && (
                    <Button size="small" href={turn.stepsUrl} target="_blank">
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
    </div>
  );
}
