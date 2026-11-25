/** Copilot 轮次线程：Collapse 消息列表 + 分析进度弹窗 + 执行轨迹抽屉。
 *
 * 从 ProcurementCopilotPage 抽出（P4 拆分，页面 < 800 行约束）。
 * 渲染口径与拆分前逐行对齐 —— 这里只搬 UI，不改行为。
 */

import { Alert, Button, Card, Collapse, Drawer, Empty, Modal, Space, Spin, Tag, Timeline, Typography } from "antd";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useTranslation } from "../../i18n";
import {
  askOf,
  shortTitle,
  STATUS_COLOR,
  type CopilotTurn,
  type TraceState,
} from "./copilotTurnModel";

const { Text } = Typography;

interface CopilotTurnThreadProps {
  /** 是否在会话视图内（home 卡片墙不渲染线程） */
  visible: boolean;
  /** 自由对话视图：无线程时显示空态 */
  showEmptyState: boolean;
  turns: CopilotTurn[];
  busy: boolean;
  trace: TraceState;
  onOpenTrace: (runId: string) => void;
  onCloseTrace: () => void;
}

export default function CopilotTurnThread({
  visible,
  showEmptyState,
  turns,
  busy,
  trace,
  onOpenTrace,
  onCloseTrace,
}: CopilotTurnThreadProps) {
  const { t } = useTranslation();

  if (!visible) return null;

  return (
    <>
      <Spin spinning={busy && turns.length === 0}>
        {turns.length === 0 ? (
          showEmptyState ? <Empty description={t("procurement.copilot.empty")} /> : null
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
                  items={[
                    {
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
                                  if (turn.runId) onOpenTrace(turn.runId);
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
                    },
                  ]}
                />
              </div>
            ))}
          </Space>
        )}
      </Spin>
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
        onClose={onCloseTrace}
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
    </>
  );
}
