import { memo } from "react";
import { Alert, Button, Card, Collapse, Space, Tag, Typography } from "antd";
import { FilePdfOutlined } from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import QueryPlanCard from "./QueryPlanCard";
import MultiStepPlanCard from "./MultiStepPlanCard";
import SqlPreview from "./SqlPreview";
import ChartRenderer from "./ChartRenderer";
import TermDictionaryButton from "./TermDictionaryButton";
import Supplier360Card from "./Supplier360Card";
import SupplierRiskCard from "./SupplierRiskCard";
import GraphTraversalCard from "./GraphTraversalCard";
import AgentResponseCard from "./AgentResponseCard";
import SuggestedAgentCard from "./SuggestedAgentCard";
import type { ChatMessage } from "../../types/chat";
import { useTranslation } from "../../i18n";

const { Paragraph } = Typography;

interface MessageItemProps {
  message: ChatMessage;
  /** PDF 导出进行中（控制单条按钮 disabled 状态）。 */
  exporting?: boolean;
  /** 单条问答导出回调：参数是 message.dbMessageId（后端 SessionMessage 主键）。
   *  仅当 message.dbMessageId 已回填时 MessageItem 才会渲染入口按钮。 */
  onExportSingleTurn?: (dbMessageId: number) => void;
}

function MessageItem({ message, exporting = false, onExportSingleTurn }: MessageItemProps) {
  const { t } = useTranslation();
  const isUser = message.role === "user";

  if (isUser) {
    return (
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
        <Card size="small" style={{ maxWidth: "95%", background: "#e6f4ff" }}>
          <Paragraph style={{ margin: 0, whiteSpace: "pre-wrap" }}>{message.content}</Paragraph>
        </Card>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", justifyContent: "flex-start", marginBottom: 12 }}>
      <Card size="small" style={{ maxWidth: "95%", background: "#fafafa" }}>
        {message.isError ? (
          <>
            <Alert type="error" showIcon message={message.content} />
            {message.errorDetail ? (
              <div style={{ marginTop: 8 }}>
                <Collapse
                  size="small"
                  items={[
                    {
                      key: "detail",
                      label: t("messageItem.validationDetail"),
                      children: (
                        <Paragraph style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                          {message.errorDetail}
                        </Paragraph>
                      ),
                    },
                  ]}
                />
              </div>
            ) : null}
            <div style={{ marginTop: 8 }}>
              <TermDictionaryButton />
            </div>
          </>
        ) : (
          <div>
            {message.isStreaming ? (
              // 流式期间行高与 markdown 渲染对齐（1.7），完成后切换渲染无视觉跳动（审查 LOW-2）
              <Paragraph style={{ margin: 0, whiteSpace: "pre-wrap", lineHeight: 1.7 }}>
                {message.content}
                <span className="typing-cursor" aria-label={t("messageItem.typing")} />
              </Paragraph>
            ) : (
              <div className="markdown-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
              </div>
            )}
            {message.queryPlan && !message.isStreaming ? (
              <div style={{ marginTop: 8 }}>
                <QueryPlanCard plan={message.queryPlan} dataQuality={message.dataQuality ?? null} />
              </div>
            ) : null}
            {message.steps && message.steps.length ? (
              <div style={{ marginTop: 8 }}>
                <MultiStepPlanCard steps={message.steps} currentStepIndex={message.currentStepIndex} />
              </div>
            ) : null}
            {message.sql ? (
              <div style={{ marginTop: 8 }}>
                <SqlPreview sql={message.sql} />
              </div>
            ) : null}
            {message.chartType && message.chartOption ? (
              <div style={{ marginTop: 8 }}>
                <ChartRenderer
                  chartType={message.chartType}
                  chartOption={message.chartOption}
                  data={message.data}
                />
              </div>
            ) : null}
            {/* Phase 5.3：供应商 360° ADS 视图卡片（仅 intent=supplier_360 + 命中 supplier_key 时回填）。
                按字段存在性路由，不依赖 message.intent 兜底（意图可能是 chitchat/clarify 等其他）。 */}
            {message.supplier360 ? (
              <div style={{ marginTop: 8 }}>
                <Supplier360Card data={message.supplier360} />
              </div>
            ) : null}
            {/* Phase 5.4：供应商风险 Agent 卡片（仅 intent=supplier_risk + 命中 supplier_key 时回填） */}
            {message.supplierRisk ? (
              <div style={{ marginTop: 8 }}>
                <SupplierRiskCard data={message.supplierRisk} />
              </div>
            ) : null}
            {/* Phase 6.3：知识图谱多跳推理卡片（仅 intent=graph_reasoning + 命中起点时回填） */}
            {message.graphTraversal ? (
              <div style={{ marginTop: 8 }}>
                <GraphTraversalCard data={message.graphTraversal} />
              </div>
            ) : null}
            {/* Phase 6.4：Agent 运行时响应卡片（仅 intent=agent_run + 运行时成功时回填） */}
            {message.agentRun ? (
              <div style={{ marginTop: 8 }}>
                <AgentResponseCard data={message.agentRun} />
              </div>
            ) : null}
            {/* Phase 7 G4：未指名 Agent 语义路由建议卡片（中置信命中时随查询响应回填） */}
            {message.suggestedAgent ? (
              <div style={{ marginTop: 8 }}>
                <SuggestedAgentCard data={message.suggestedAgent} />
              </div>
            ) : null}
            {message.tokensUsed !== undefined ||
            message.cost !== undefined ||
            message.modelName ||
            message.affinityStatus ? (
              <Space style={{ marginTop: 8 }} size={4} wrap>
                {message.tokensUsed !== undefined || message.cost !== undefined ? (
                  <>
                    <Tag>Tokens: {message.tokensUsed ?? 0}</Tag>
                    <Tag>{t("messageItem.cost", { amount: Number(message.cost ?? 0).toFixed(6) })}</Tag>
                  </>
                ) : null}
                {message.modelName ? <Tag color="blue">{t("messageItem.model", { name: message.modelName })}</Tag> : null}
                {/* Phase 7：会话亲和性锁定 badge */}
                {message.affinityStatus ? (
                  <Tag color="gold">
                    {t("messageItem.affinityLocked", {
                      model: message.affinityStatus.lockedModel,
                      turns: message.affinityStatus.remainingTurns,
                    })}
                  </Tag>
                ) : null}
              </Space>
            ) : null}
            {/* PDF 导出此条按钮：仅当 message 已持久化（带 dbMessageId）时显示。
                当前会话消息默认无 dbMessageId（chatStore 暂不维护实时 db id 映射，
                避免引入额外后端查询），因此默认仅"全局导出"按钮生效；后续单独 PR
                增加"sendMessage 完成后回填 dbMessageId"逻辑后再放开单条入口。 */}
            {!message.isStreaming && onExportSingleTurn && message.dbMessageId !== undefined ? (
              <div style={{ marginTop: 8, textAlign: "right" }}>
                <Button
                  size="small"
                  type="link"
                  icon={<FilePdfOutlined />}
                  loading={exporting}
                  onClick={() => onExportSingleTurn(message.dbMessageId as number)}
                  aria-label={t("chat.exportPdf.singleAriaLabel")}
                >
                  {t("chat.exportPdf.singleButton")}
                </Button>
              </div>
            ) : null}
          </div>
        )}
      </Card>
    </div>
  );
}

// memo 避免父组件重渲染时对相同 content 重复解析 markdown（审查 MEDIUM-3）
export default memo(MessageItem);