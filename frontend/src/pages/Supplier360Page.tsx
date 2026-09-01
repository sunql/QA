import { useCallback, useState } from "react";
import { Alert, Button, Card, Space, Typography } from "antd";
import Supplier360Card from "../components/chat/Supplier360Card";
import EntityAutoComplete from "../components/common/EntityAutoComplete";
import { getSupplier360 } from "../api/supplier";
import { useTranslation } from "../i18n";
import type { Supplier360Read } from "../types/supplier";

const { Title, Paragraph } = Typography;

/**
 * 供应商 360° 直接入口页（Phase 5.3）。
 *
 * 用途：
 * - 跳过 Chat NL2SQL，直接通过 /api/v1/supplier-360/{key} 拉取聚合视图。
 * - 输入 enterprise_key → 拉取并渲染 Supplier360Card；失败显示通用 NotFound 消息。
 *
 * 与 Chat 中的 supplier360 卡片共享 Supplier360Card 组件（保证 UI 一致）。
 * 后续可挂权限/ACL（Phase 5.4+）；当前与 supplier_360 API 一致——仅 getCurrentUser 鉴权。
 */
export default function Supplier360Page() {
  const { t } = useTranslation();
  // Phase 6.x：state 改为 enterprise_key: number | null，由 EntityAutoComplete 维护
  const [supplierKey, setSupplierKey] = useState<number | null>(null);
  const [data, setData] = useState<Supplier360Read | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleQuery = useCallback(async () => {
    if (supplierKey === null) {
      setError(t("supplier360Page.invalidKey"));
      setData(null);
      return;
    }
    setLoading(true);
    setError(null);
    setData(null);
    try {
      const result = await getSupplier360(supplierKey);
      setData(result);
    } catch (e: unknown) {
      // 与 4.5 ACL 通用消息原则一致：not-found / forbidden 统一文案，避免侧信道
      const msg = e instanceof Error ? e.message : String(e);
      setError(
        msg.includes("404") || msg.includes("不存在")
          ? t("supplier360Page.notFound", { key: supplierKey })
          : t("supplier360Page.requestFailed", { message: msg }),
      );
    } finally {
      setLoading(false);
    }
  }, [supplierKey, t]);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card size="small">
        <Title level={4} style={{ marginTop: 0 }}>
          {t("supplier360Page.title")}
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 12 }}>
          {t("supplier360Page.hint")}
        </Paragraph>
        <Space>
          {/* Phase 6.x：AutoComplete 替代纯数字 Input，按名称/编码搜索 */}
          <EntityAutoComplete
            value={supplierKey}
            onChange={setSupplierKey}
            entityType="SUPPLIER"
            placeholder={t("supplier360Page.placeholder") as string}
            onPressEnter={handleQuery}
          />
          <Button type="primary" loading={loading} onClick={handleQuery}>
            {t("supplier360Page.query")}
          </Button>
        </Space>
      </Card>

      {error ? <Alert type="error" showIcon message={error} /> : null}

      {data ? <Supplier360Card data={data} /> : null}
    </Space>
  );
}