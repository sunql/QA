import { Card, Space, Tag, Typography } from "antd";
import type { AgentSuggestion } from "../../types/chat";
import { useTranslation } from "../../i18n";

const { Text, Paragraph } = Typography;

interface SuggestedAgentCardProps {
  data: AgentSuggestion;
}

/** 置信度 → 百分比展示（0.4 → 40%）。 */
function formatConfidence(confidence: number): string {
  return `${Math.round(confidence * 100)}%`;
}

/**
 * 未指名 Agent 语义路由建议卡片（Phase 7 G4 feat-agent-semantic-routing）。
 *
 * 中置信（0.4 ≤ confidence < 0.7）语义路由命中时，随 QUERY/NEW_QUERY 响应
 * 附带；MessageItem 按 suggestedAgent 字段存在性路由到此卡片渲染。
 *
 * 展示：推荐 Agent 编码 + 置信度 Tag + 理由。用户可直接在输入框以显式指名
 * 方式（如「用 SUPPLIER_RISK_AGENT 评估供应商 100001」）触发执行。
 */
export default function SuggestedAgentCard({ data }: SuggestedAgentCardProps) {
  const { t } = useTranslation();
  const { recommendedAgentCode, confidence, reason } = data;

  return (
    <Card
      size="small"
      title={
        <Space wrap>
          <Text strong>{t("suggestedAgent.title")}</Text>
          <Tag color="geekblue">{recommendedAgentCode}</Tag>
          {/* 卡片只会收到中置信 0.4（>= 0.7 已直接调度不落卡片），固定默认色即可 */}
          <Tag>
            {t("suggestedAgent.confidence", {
              percent: formatConfidence(confidence),
            })}
          </Tag>
        </Space>
      }
      style={{ background: "#f5f5f5", marginTop: 8 }}
    >
      <Paragraph style={{ marginBottom: 0, fontSize: 13 }}>{reason}</Paragraph>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {t("suggestedAgent.hint")}
      </Text>
    </Card>
  );
}
