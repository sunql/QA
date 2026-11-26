/** 到货跟踪场景的欢迎态：**本期业务现状**统计卡。
 *
 * 与 `PendingDeliveryWelcome` 同构（待到货场景进场景即出数，零 LLM 成本）：
 * 直接读 `/procurement/delivery-tracking/overview`（后端只读 SQL，本期 24 日结算周期），
 * 不走编排 run ⇒ 进场景即出数，不烧 token、不等 worker。
 *
 * 口径：本期 = 企业默认月度结算周期（每月 24 日 → 次月 24 日），与场景 chip
 * 「本期到货概览（24 日周期）」同一口径。
 */

import { Alert, Card, Col, Row, Statistic, Typography } from "antd";
import { useTranslation } from "../../i18n";
import type { DeliveryTrackingOverviewResponse } from "../../api/procurementCopilot";

const { Text } = Typography;

function fmtQty(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 }).format(value);
}

interface DeliveryTrackingWelcomeProps {
  overview: {
    loading: boolean;
    error: string | null;
    data: DeliveryTrackingOverviewResponse | null;
  };
}

export default function DeliveryTrackingWelcome({ overview }: DeliveryTrackingWelcomeProps) {
  const { t } = useTranslation();

  return (
    <>
      <Text type="secondary" style={{ letterSpacing: 2, fontSize: 12, display: "block" }}>
        DELIVERY TRACKING
      </Text>
      <Typography.Title level={3} style={{ marginTop: 4, marginBottom: 4 }}>
        {t("procurement.copilot.deliveryHeroGreeting")}
      </Typography.Title>
      <Text type="secondary">{t("procurement.copilot.deliveryHeroSubtitle")}</Text>
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} md={16}>
          <Card
            size="small"
            loading={overview.loading}
            title={t("procurement.copilot.deliveryBusinessNow")}
          >
            {overview.error ? (
              <Alert type="warning" showIcon message={overview.error} />
            ) : overview.data ? (
              <>
                <Row gutter={12}>
                  <Col span={6}>
                    <Statistic
                      title={t("procurement.copilot.deliveryStatOrderRate")}
                      value={overview.data.summary.orderArrivalRate}
                      precision={2}
                      suffix="%"
                    />
                  </Col>
                  <Col span={6}>
                    <Statistic
                      title={t("procurement.copilot.deliveryStatQtyRate")}
                      value={overview.data.summary.qtyArrivalRate}
                      precision={2}
                      suffix="%"
                    />
                  </Col>
                  <Col span={6}>
                    <Statistic
                      title={t("procurement.copilot.deliveryStatOrders")}
                      value={overview.data.summary.totalOrders}
                    />
                  </Col>
                  <Col span={6}>
                    <Statistic
                      title={t("procurement.copilot.deliveryStatNotArrived")}
                      value={overview.data.summary.notArrivedOrders}
                      valueStyle={
                        overview.data.summary.notArrivedOrders > 0 ? { color: "#cf1322" } : undefined
                      }
                    />
                  </Col>
                </Row>
                <Text type="secondary" style={{ display: "block", marginTop: 8 }}>
                  {t("procurement.copilot.deliveryStatWindow")}:{" "}
                  {overview.data.window.dateFrom} ~ {overview.data.window.dateTo}
                  {" · "}
                  {t("procurement.copilot.deliveryStatArrivedBreakdown")}:{" "}
                  {t("procurement.copilot.deliveryStatAllArrived")}{" "}
                  {overview.data.summary.allArrivedOrders} /{" "}
                  {t("procurement.copilot.deliveryStatPartial")}{" "}
                  {overview.data.summary.partialArrivedOrders} /{" "}
                  {t("procurement.copilot.deliveryStatNotArrived")}{" "}
                  {overview.data.summary.notArrivedOrders}
                </Text>
                {overview.data.bySupplier.length > 0 && (
                  <Text type="secondary" style={{ display: "block", marginTop: 4 }}>
                    {t("procurement.copilot.deliveryStatTopSuppliers")}:{" "}
                    {overview.data.bySupplier
                      .slice(0, 3)
                      .map(
                        (item) =>
                          `${item.supplierName || item.supplierCode}(未到货 ${item.notArrivedOrders})`,
                      )
                      .join("、")}
                  </Text>
                )}
                {overview.data.summary.excludedRows > 0 && (
                  <Text type="secondary" style={{ display: "block", marginTop: 4 }}>
                    {t("procurement.copilot.deliveryStatExcluded")}:{" "}
                    {fmtQty(overview.data.summary.excludedRows)}
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
