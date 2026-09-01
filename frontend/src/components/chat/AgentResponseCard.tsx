import type { ReactNode } from "react";
import { Card, Collapse, Space, Tag, Typography } from "antd";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Supplier360Card from "./Supplier360Card";
import SupplierRiskCard from "./SupplierRiskCard";
import GraphTraversalCard from "./GraphTraversalCard";
import type { AgentRunRead } from "../../types/agentRuntime";
import type { Supplier360Read } from "../../types/supplier";
import type { SupplierRiskRead } from "../../types/supplierRisk";
import type { GraphTraversalRead } from "../../types/graphTraversal";
import { useTranslation } from "../../i18n";

const { Text, Title, Paragraph } = Typography;

interface AgentResponseCardProps {
  data: AgentRunRead;
}

/** 已内置富卡片渲染的 Agent 工具（其余工具回退 JSON 明细折叠）。 */
const RICH_TOOLS = new Set(["supplier_360", "supplier_risk", "graph_traverse"]);

/**
 * Agent 运行时响应卡片（Phase 6.4 feat-agent-runtime-mvp）。
 *
 * - 头部：agentName + tool Tag + owner + executedAt
 * - 正文：工具 handler 生成的 answer（markdown）+ 按 tool 名内嵌既有富卡片：
 *     supplier_360 → Supplier360Card / supplier_risk → SupplierRiskCard /
 *     graph_traverse → GraphTraversalCard（result 即对应 DTO 的 model_dump，
 *     camelCase 形状一致，复用组件保证 UI 统一）
 *   - 未识别的工具：Collapse 内 <pre> JSON 明细（不做盲目结构假设）
 * - 底部：Tokens / Cost / LLM 模型（与 ChatMessage 一致）
 */
export default function AgentResponseCard({ data }: AgentResponseCardProps) {
  const { t } = useTranslation();
  const {
    agentCode,
    agentName,
    agentOwner,
    tool,
    result,
    answer,
    tokensUsed,
    promptTokens,
    completionTokens,
    cost,
    llmModelName,
    executedAt,
  } = data;

  let richBody: ReactNode = null;
  if (tool === "supplier_360") {
    richBody = <Supplier360Card data={result as unknown as Supplier360Read} />;
  } else if (tool === "supplier_risk") {
    richBody = <SupplierRiskCard data={result as unknown as SupplierRiskRead} />;
  } else if (tool === "graph_traverse") {
    richBody = <GraphTraversalCard data={result as unknown as GraphTraversalRead} />;
  }

  return (
    <Card
      size="small"
      title={
        <Space wrap>
          <Title level={5} style={{ margin: 0 }}>
            {agentName || agentCode}
          </Title>
          <Tag color="geekblue">{tool}</Tag>
          {agentOwner ? <Tag>{t("agentRuntime.owner", { owner: agentOwner })}</Tag> : null}
          <Text type="secondary" style={{ fontSize: 12 }}>
            {agentCode}
          </Text>
        </Space>
      }
      extra={
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("agentRuntime.executedAt", { time: executedAt })}
        </Text>
      }
      style={{ background: "#f0f5ff", marginTop: 8 }}
    >
      <Paragraph style={{ marginBottom: 8 }}>
        <div className="markdown-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown>
        </div>
      </Paragraph>

      {richBody}

      {!RICH_TOOLS.has(tool) ? (
        <Collapse
          size="small"
          items={[
            {
              key: "raw",
              label: t("agentRuntime.rawDetail"),
              children: (
                <pre
                  style={{
                    margin: 0,
                    maxHeight: 320,
                    overflow: "auto",
                    fontSize: 12,
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-all",
                  }}
                >
                  {JSON.stringify(result, null, 2)}
                </pre>
              ),
            },
          ]}
        />
      ) : null}

      <Space size={4} wrap style={{ marginTop: 8 }}>
        <Tag>
          Tokens: {tokensUsed} (P {promptTokens} / C {completionTokens})
        </Tag>
        <Tag>{t("agentRuntime.cost", { amount: cost.toFixed(6) })}</Tag>
        {llmModelName ? <Tag color="blue">{llmModelName}</Tag> : null}
      </Space>
    </Card>
  );
}
