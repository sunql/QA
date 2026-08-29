import { useCallback, useEffect, useState } from "react";
import { Button, Card, Col, Row, Space, Tag, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { getServiceStatus } from "../api/serviceStatus";
import type {
  ServiceHealth,
  ServiceStatus,
  ServiceStatusResponse,
} from "../types/serviceStatus";
import { useTranslation } from "../i18n";

const { Text } = Typography;

// 状态 → antd Tag 颜色
const STATUS_COLOR: Record<ServiceHealth, string> = {
  up: "green",
  down: "red",
  not_configured: "orange",
};

// 服务标识 → i18n 键（后端 name 为机器标识，展示名走前端 i18n）
const SERVICE_LABEL_KEY: Record<string, string> = {
  postgresql: "forms.serviceStatus.services.postgresql",
  neo4j: "forms.serviceStatus.services.neo4j",
  milvus: "forms.serviceStatus.services.milvus",
  embedding: "forms.serviceStatus.services.embedding",
};

export default function ServiceStatusPage() {
  const { t } = useTranslation();
  const [response, setResponse] = useState<ServiceStatusResponse | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getServiceStatus();
      setResponse(data);
    } catch {
      // 错误已由拦截器提示
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const statusLabel = (status: ServiceHealth): string => {
    if (status === "up") return t("forms.serviceStatus.status.up");
    if (status === "down") return t("forms.serviceStatus.status.down");
    return t("forms.serviceStatus.status.notConfigured");
  };

  const renderDetail = (s: ServiceStatus) => {
    if (s.status === "down" && s.detail) {
      return <Text type="danger">{s.detail}</Text>;
    }
    if (s.status === "not_configured" && s.detail) {
      return <Text type="secondary">{s.detail}</Text>;
    }
    return null;
  };

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
          {t("common.refresh")}
        </Button>
      </Space>

      <Row gutter={[12, 12]}>
        {(response?.services ?? []).map((s) => (
          <Col xs={24} sm={12} lg={6} key={s.name}>
            <Card
              size="small"
              loading={loading}
              title={t(SERVICE_LABEL_KEY[s.name] ?? s.name)}
            >
              <Space direction="vertical" size={4} style={{ width: "100%" }}>
                <Tag color={STATUS_COLOR[s.status]}>{statusLabel(s.status)}</Tag>
                {s.latencyMs !== null ? (
                  <Text type="secondary">
                    {t("forms.serviceStatus.latency", { ms: s.latencyMs })}
                  </Text>
                ) : null}
                {s.endpoint ? (
                  <Text type="secondary" ellipsis={{ tooltip: s.endpoint }} style={{ width: "100%" }}>
                    {s.endpoint}
                  </Text>
                ) : null}
                {renderDetail(s)}
              </Space>
            </Card>
          </Col>
        ))}
      </Row>

      {response?.checkedAt ? (
        <Text type="secondary" style={{ display: "block", marginTop: 16 }}>
          {t("forms.serviceStatus.checkedAt", {
            time: new Date(response.checkedAt).toLocaleString(),
          })}
        </Text>
      ) : null}
    </div>
  );
}
