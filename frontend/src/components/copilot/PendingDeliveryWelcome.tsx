/** 待到货场景的欢迎态：**业务现状统计**。
 *
 * 从 ProcurementCopilotPage 抽出（P4 拆分）。
 *
 * 推荐问题 chips **不在这里** —— chips 是通用能力（任何场景配了 chips 都
 * 该显示），见 CopilotSuggestedQuestions；本组件只负责待到货专属的现状统计。
 */

import { Alert, Card, Col, Row, Statistic, Typography } from "antd";
import { useTranslation } from "../../i18n";
import type { PendingDeliverySummaryResponse } from "../../api/procurementCopilot";

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
}

export default function PendingDeliveryWelcome({ summary }: PendingDeliveryWelcomeProps) {
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
      </Row>
    </>
  );
}
