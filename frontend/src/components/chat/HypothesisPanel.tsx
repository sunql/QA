import { Button, Card, Collapse, Space, Tag, Tooltip, Typography } from "antd";
import { CaretRightOutlined, CodeOutlined, QuestionCircleOutlined } from "@ant-design/icons";
import type { HypothesisView } from "../../types/chat";
import { useTranslation } from "../../i18n";

const { Paragraph, Text } = Typography;

// 展示上限：与后端 HYPOTHESIS_MAX_COUNT 对齐（前端再裁一次防御）
const DISPLAY_MAX_COUNT = 3;

interface HypothesisPanelProps {
  hypotheses: HypothesisView[];
  /** 「验证」回调：把 verificationSql 作为用户问题走既有发送链路（不直接执行 SQL）。 */
  onVerify: (verificationSql: string) => void;
}

/**
 * v3.1 B6（M7 Hypothesis Hook）：答案下方的「可能原因」区块。
 *
 * 每条假设：statement 文本 + driver 小徽标 + 可折叠「验证 SQL」代码块（只读展示）
 * +「验证」按钮（把 SQL 填入聊天输入框发送，复用既有 QUERY 链路——SQL Guard 自然生效）。
 * 无假设时由调用方整块隐藏，本组件不做空渲染。
 */
export default function HypothesisPanel({ hypotheses, onVerify }: HypothesisPanelProps) {
  const { t } = useTranslation();
  const items = hypotheses.slice(0, DISPLAY_MAX_COUNT);
  if (items.length === 0) return null;

  return (
    <Card
      size="small"
      style={{ marginTop: 8, background: "#f6fffb", border: "1px solid #b5f5ec" }}
      title={
        <Space size={4}>
          <QuestionCircleOutlined />
          <span>{t("chat.hypothesis.title")}</span>
        </Space>
      }
      data-testid="hypothesis-panel"
    >
      {items.map((h) => (
        <div key={h.id} style={{ marginBottom: 8 }} data-testid="hypothesis-item">
          <Space size={6} align="start" wrap>
            <CaretRightOutlined style={{ color: "#13c2c2", marginTop: 4 }} />
            <Paragraph style={{ margin: 0, flex: 1 }}>{h.statement}</Paragraph>
            {h.driver ? (
              <Tooltip title={t("chat.hypothesis.driverTooltip")}>
                <Tag color="cyan" style={{ marginRight: 0 }}>{h.driver}</Tag>
              </Tooltip>
            ) : null}
          </Space>
          <Space size={8} style={{ marginTop: 4 }}>
            <Button
              size="small"
              type="link"
              icon={<CodeOutlined />}
              onClick={() => onVerify(h.verificationSql)}
            >
              {t("chat.hypothesis.verify")}
            </Button>
          </Space>
          <Collapse
            size="small"
            ghost
            items={[
              {
                key: "sql",
                label: t("chat.hypothesis.sqlDetail"),
                children: (
                  <Paragraph code style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                    {h.verificationSql}
                  </Paragraph>
                ),
              },
            ]}
          />
        </div>
      ))}
      <Text type="secondary" style={{ fontSize: 12 }}>
        {t("chat.hypothesis.disclaimer")}
      </Text>
    </Card>
  );
}
