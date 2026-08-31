import { useCallback, useState } from "react";
import { Alert, Button, Card, Input, Space, Typography } from "antd";
import SupplierRiskCard from "../components/chat/SupplierRiskCard";
import { getSupplierRisk } from "../api/supplierRisk";
import { useTranslation } from "../i18n";
import type { SupplierRiskRead } from "../types/supplierRisk";

const { Title, Paragraph } = Typography;

/**
 * 供应商风险 Agent 直接入口页（Phase 5.4）。
 *
 * 用途：
 * - 跳过 Chat NL2SQL，直接通过 /api/v1/supplier-risk/{key} 取风险等级。
 * - 输入 enterprise_key → 拉取并渲染 SupplierRiskCard；失败显示通用 NotFound 消息。
 *
 * 与 Chat 中的 supplierRisk 卡片共享 SupplierRiskCard 组件（保证 UI 一致）。
 */
export default function SupplierRiskPage() {
  const { t } = useTranslation();
  const [supplierKey, setSupplierKey] = useState("");
  const [data, setData] = useState<SupplierRiskRead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleQuery = useCallback(async () => {
    const trimmed = supplierKey.trim();
    if (!/^\d{5,9}$/.test(trimmed)) {
      setError(t("supplierRiskPage.invalidKey"));
      setData(null);
      return;
    }
    setLoading(true);
    setError(null);
    setData(null);
    try {
      const result = await getSupplierRisk(Number(trimmed));
      setData(result);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(
        msg.includes("404") || msg.includes("不存在")
          ? t("supplierRiskPage.notFound", { key: trimmed })
          : t("supplierRiskPage.requestFailed", { message: msg }),
      );
    } finally {
      setLoading(false);
    }
  }, [supplierKey, t]);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card size="small">
        <Title level={4} style={{ marginTop: 0 }}>
          {t("supplierRiskPage.title")}
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 12 }}>
          {t("supplierRiskPage.hint")}
        </Paragraph>
        <Space>
          <Input
            value={supplierKey}
            onChange={(e) => setSupplierKey(e.target.value)}
            onPressEnter={handleQuery}
            placeholder={t("supplierRiskPage.placeholder") as string}
            style={{ width: 240 }}
            inputMode="numeric"
            aria-label={t("supplierRiskPage.placeholder") as string}
          />
          <Button type="primary" loading={loading} onClick={handleQuery}>
            {t("supplierRiskPage.query")}
          </Button>
        </Space>
      </Card>

      {error ? <Alert type="error" showIcon message={error} /> : null}

      {data ? <SupplierRiskCard data={data} /> : null}
    </Space>
  );
}