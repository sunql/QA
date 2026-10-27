import { Button, Space, Switch, Table, Tag } from "antd";
import type { TableColumnsType } from "antd";
import { useTranslation } from "../../i18n";
import type { AgentOrchestrationProfileRead } from "../../types/agentOrchestration";

export interface OrchestrationProfileTableProps {
  profiles: AgentOrchestrationProfileRead[];
  loading: boolean;
  /** 编辑：把整条配置交给页面层打开弹窗。 */
  onEdit: (profile: AgentOrchestrationProfileRead) => void;
  /** 切换启用状态：走 toggleOrchestration，不走 update。 */
  onToggle: (profile: AgentOrchestrationProfileRead) => void;
}

/**
 * 编排配置列表（纯展示）：列定义与渲染都在这里，
 * 数据获取与写操作留在页面层。
 */
export default function OrchestrationProfileTable({
  profiles,
  loading,
  onEdit,
  onToggle,
}: OrchestrationProfileTableProps) {
  const { t } = useTranslation();

  const columns: TableColumnsType<AgentOrchestrationProfileRead> = [
    {
      title: t("agentOrchestration.columns.agentCode"),
      dataIndex: "agentCode",
      key: "agentCode",
    },
    {
      title: t("agentOrchestration.columns.tools"),
      key: "tools",
      render: (_value, record) =>
        t("agentOrchestration.toolCount", { count: record.bindings.length }),
    },
    {
      title: t("agentOrchestration.columns.enabled"),
      key: "enabled",
      render: (_value, record) => (
        <Space>
          <Switch
            checked={record.enabled}
            aria-label={t("agentOrchestration.columns.enabled")}
            onChange={() => onToggle(record)}
          />
          <Tag color={record.enabled ? "green" : "default"}>
            {record.enabled
              ? t("agentOrchestration.status.enabled")
              : t("agentOrchestration.status.disabled")}
          </Tag>
        </Space>
      ),
    },
    {
      title: t("agentOrchestration.columns.revision"),
      dataIndex: "revision",
      key: "revision",
    },
    {
      title: t("agentOrchestration.columns.actions"),
      key: "actions",
      render: (_value, record) => (
        <Button size="small" onClick={() => onEdit(record)}>
          {t("agentOrchestration.editButton")}
        </Button>
      ),
    },
  ];

  return (
    <Table<AgentOrchestrationProfileRead>
      rowKey="agentCode"
      columns={columns}
      dataSource={profiles}
      loading={loading}
      pagination={false}
      locale={{ emptyText: t("agentOrchestration.empty.profiles") }}
    />
  );
}
