import { Button, Card, Space, Typography } from "antd";
import { useTranslation } from "../../i18n";

export interface OrchestrationProfileHeaderProps {
  /** 打开「新建配置」弹窗。 */
  onCreate: () => void;
  /** 重新拉取配置列表。 */
  onRefresh: () => void;
}

/** 编排配置页的页头：标题、说明与新建 / 刷新按钮。 */
export default function OrchestrationProfileHeader({
  onCreate,
  onRefresh,
}: OrchestrationProfileHeaderProps) {
  const { t } = useTranslation();
  return (
    <Card size="small">
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {t("agentOrchestration.title")}
        </Typography.Title>
        <Typography.Paragraph type="secondary">
          {t("agentOrchestration.subtitle")}
        </Typography.Paragraph>
        <Space>
          <Button type="primary" onClick={onCreate}>
            {t("agentOrchestration.createButton")}
          </Button>
          <Button onClick={onRefresh}>
            {t("agentOrchestration.refreshButton")}
          </Button>
        </Space>
      </Space>
    </Card>
  );
}
