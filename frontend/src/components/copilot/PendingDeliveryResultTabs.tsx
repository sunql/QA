/** 待到货结果 Tabs（P4 拆分）：会话内多结构化结果用 Tabs 切换，
 * 内容只渲染激活项（滚动不会看到其他结果）。
 */

import { Button, Card, Empty, Space, Tabs, Tooltip, Typography } from "antd";
import { TableOutlined } from "@ant-design/icons";
import { useTranslation } from "../../i18n";
import type { CopilotTurn } from "./copilotTurnModel";
import PendingDeliveryResultCard, {
  type PendingDeliveryResult,
} from "./PendingDeliveryResultCard";

const { Text } = Typography;

export interface ResultTurn {
  turn: CopilotTurn;
  parsed: PendingDeliveryResult | null;
}

interface PendingDeliveryResultTabsProps {
  resultTurns: ResultTurn[];
  activeResultTurnId: string | null;
  onChange: (turnId: string) => void;
  onViewLines: () => void;
}

export default function PendingDeliveryResultTabs({
  resultTurns,
  activeResultTurnId,
  onChange,
  onViewLines,
}: PendingDeliveryResultTabsProps) {
  const { t } = useTranslation();
  const activeIndex = resultTurns.findIndex(
    (item) =>
      item.turn.id ===
      (activeResultTurnId ?? resultTurns[resultTurns.length - 1].turn.id),
  );
  const active = resultTurns[activeIndex >= 0 ? activeIndex : resultTurns.length - 1];
  if (!active) return null;

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
        onChange={onChange}
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
          <Button size="small" icon={<TableOutlined />} onClick={onViewLines}>
            {t("procurement.copilot.viewPendingDeliveryLines")}
          </Button>
        </>
      ) : (
        <Empty description={t("procurement.copilot.traceEmpty")} />
      )}
    </Card>
  );
}
