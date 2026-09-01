import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Input, Select, Space, Typography } from "antd";
import AgentResponseCard from "../components/chat/AgentResponseCard";
import { listAgents, runAgent } from "../api/agentRegistry";
import { useTranslation } from "../i18n";
import type { AgentDefinition } from "../types/agentRegistry";
import type { AgentRunRead } from "../types/agentRuntime";

const { Title, Paragraph } = Typography;

interface StatusError extends Error {
  status?: number;
}

/**
 * Agent 运行时直接入口页（Phase 6.4 feat-agent-runtime-mvp）。
 *
 * 用途：
 * - 跳过 Chat 意图识别，直接对已注册 Agent 调用 POST /agents/{code}/run。
 * - 选择 Agent → 输入自然语言（含供应商编码）→ 运行 → 渲染 AgentResponseCard。
 * - 失败按 HTTP status 分流（404 未注册 / 409 不可运行 / 403 无策略 / 422 参数解析失败）。
 *
 * 与 Chat 中的 agentRun 卡片共享 AgentResponseCard 组件（保证 UI 一致）。
 */
export default function AgentRuntimePage() {
  const { t } = useTranslation();
  // t 每次渲染都是新闭包；若加载 effect 依赖 [t]，配合 setAgents(rows.filter(...))
  // （.filter() 恒返回新数组引用）会形成「重渲染 → 新 t → effect 重跑」的无限循环。
  // 用 ref 持有最新 t，使加载 effect 只执行一次（[t] 陷阱见 #206 审查）。
  const tRef = useRef(t);
  tRef.current = t;
  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [agentsLoading, setAgentsLoading] = useState(false);
  const [agentCode, setAgentCode] = useState<string | undefined>(undefined);
  const [input, setInput] = useState("");
  const [data, setData] = useState<AgentRunRead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setAgentsLoading(true);
    listAgents()
      .then((rows) => {
        if (!cancelled) {
          // 仅展示 runnable Agent（Phase 6.4 SSOT 派生字段）：
          // 排除 DRAFT/DEPRECATED，以及元数据占位（status=active 但未绑定工具，
          // 如 SUPPLIER_OTD_REPORT / PROCUREMENT_COPILOT）—— 它们即使用户选中
          // 也会撞 409。这里用后端 runnable 字段做首选过滤，前端不再依赖
          // status 单一维度（status=active 不等于可运行）。
          setAgents(rows.filter((a) => a.runnable === true));
        }
      })
      .catch(() => {
        if (!cancelled) setError(tRef.current("agentRuntimePage.loadFailed"));
      })
      .finally(() => {
        if (!cancelled) setAgentsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleRun = useCallback(async () => {
    const code = agentCode;
    if (!code) {
      setError(t("agentRuntimePage.selectAgent"));
      setData(null);
      return;
    }
    const trimmed = input.trim();
    if (!trimmed) {
      setError(t("agentRuntimePage.emptyInput"));
      setData(null);
      return;
    }
    setLoading(true);
    setError(null);
    setData(null);
    try {
      const result = await runAgent(code, trimmed);
      setData(result);
    } catch (e: unknown) {
      const err = e as StatusError;
      const status = err.status;
      const msg = err instanceof Error ? err.message : String(e);
      if (status === 404) {
        setError(t("agentRuntimePage.error404", { code }));
      } else if (status === 409) {
        setError(t("agentRuntimePage.error409", { code }));
      } else if (status === 403) {
        setError(t("agentRuntimePage.error403", { code }));
      } else if (status === 422) {
        setError(t("agentRuntimePage.error422"));
      } else {
        setError(t("agentRuntimePage.requestFailed", { message: msg }));
      }
    } finally {
      setLoading(false);
    }
  }, [agentCode, input, t]);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card size="small">
        <Title level={4} style={{ marginTop: 0 }}>
          {t("agentRuntimePage.title")}
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 12 }}>
          {t("agentRuntimePage.hint")}
        </Paragraph>
        <Space wrap>
          <Select
            value={agentCode}
            onChange={setAgentCode}
            placeholder={t("agentRuntimePage.selectPlaceholder") as string}
            loading={agentsLoading}
            style={{ width: 280 }}
            options={agents.map((a) => ({
              value: a.agentCode,
              label: `${a.agentCode}（${a.agentName}）`,
            }))}
            aria-label={t("agentRuntimePage.selectPlaceholder") as string}
          />
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onPressEnter={handleRun}
            placeholder={t("agentRuntimePage.inputPlaceholder") as string}
            style={{ width: 360 }}
            aria-label={t("agentRuntimePage.inputPlaceholder") as string}
          />
          <Button type="primary" loading={loading} onClick={handleRun}>
            {t("agentRuntimePage.run")}
          </Button>
        </Space>
      </Card>

      {error ? <Alert type="error" showIcon message={error} /> : null}

      {data ? <AgentResponseCard data={data} /> : null}
    </Space>
  );
}
