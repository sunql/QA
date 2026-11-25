/** 待到货场景的欢迎态：业务现状统计 + 推荐问题 chips。
 *
 * 从 ProcurementCopilotPage 抽出（P4 拆分）。chips 来自场景配置
 * （copilot_scenario.chips），点击 = 预设槽位组合直接发送（槽位值随
 * chip 传，不走 state —— setXxx 是异步的，闭包里会拿到旧值，
 * 2026-11-24 实测）。
 */

import { Alert, Button, Card, Col, Row, Space, Statistic, Typography } from "antd";
import { ArrowRightOutlined } from "@ant-design/icons";
import { useTranslation } from "../../i18n";
import type { PendingDeliverySummaryResponse } from "../../api/procurementCopilot";
import type { CopilotScenarioChip } from "../../api/copilotScenario";

const { Text } = Typography;

function fmtOpenQty(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(value);
}

interface PendingDeliveryWelcomeProps {
  summary: {
    loading: boolean;
    error: string | null;
    data: PendingDeliverySummaryResponse | null;
  };
  chips: CopilotScenarioChip[];
  disabled: boolean;
  onChipClick: (chip: CopilotScenarioChip) => void;
}

export default function PendingDeliveryWelcome({
  summary,
  chips,
  disabled,
  onChipClick,
}: PendingDeliveryWelcomeProps) {
  const { t } = useTranslation();

  return (
    <>
      <Text type="secondary" style={{ letterSpacing: 2, fontSize: 12, display: "block" }}>
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
            loading={summary.loading}
            title={t("procurement.copilot.businessNow")}
          >
            {summary.error ? (
              <Alert type="warning" showIcon message={summary.error} />
            ) : summary.data ? (
              <>
                <Row gutter={12}>
                  <Col span={6}>
                    <Statistic title={t("procurement.copilot.statOpenLines")} value={summary.data.summary.openLines} />
                  </Col>
                  <Col span={6}>
                    <Statistic title={t("procurement.copilot.statOpenQty")} value={summary.data.summary.openQty} precision={2} />
                  </Col>
                  <Col span={6}>
                    <Statistic title={t("procurement.copilot.statOverdue")} value={summary.data.summary.overdueLines} valueStyle={summary.data.summary.overdueLines > 0 ? { color: "#cf1322" } : undefined} />
                  </Col>
                  <Col span={6}>
                    <Statistic title={t("procurement.copilot.statExcluded")} value={summary.data.summary.excludedRows} />
                  </Col>
                </Row>
                {summary.data.bySupplier.length > 0 && (
                  <Text type="secondary" style={{ display: "block", marginTop: 8 }}>
                    {t("procurement.copilot.statTopSuppliers")}:{" "}
                    {summary.data.bySupplier
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
              {chips.map((chip) => (
                <Button
                  key={chip.label}
                  block
                  style={{ textAlign: "left", display: "flex", justifyContent: "space-between", alignItems: "center" }}
                  disabled={disabled}
                  onClick={() => onChipClick(chip)}
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
  );
}
