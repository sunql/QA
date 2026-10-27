import { Button, Space, Switch, Tag } from "antd";
import type { TableColumnsType } from "antd";
import type { AgentOrchestrationProfileRead } from "../../types/agentOrchestration";
import type { Translate } from "./task/errors";

/** 启用状态单元格：开关 + 状态标签。 */
function EnabledCell({
  record,
  t,
  onToggle,
}: {
  record: AgentOrchestrationProfileRead;
  t: Translate;
  onToggle: (profile: AgentOrchestrationProfileRead) => void;
}) {
  return (
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
  );
}

/** 操作列：编辑按钮。 */
function ActionsCell({
  record,
  t,
  onEdit,
}: {
  record: AgentOrchestrationProfileRead;
  t: Translate;
  onEdit: (profile: AgentOrchestrationProfileRead) => void;
}) {
  return (
    <Button size="small" onClick={() => onEdit(record)}>
      {t("agentOrchestration.editButton")}
    </Button>
  );
}

/** 编排配置列表的列定义。 */
export interface OrchestrationProfileColumnsOptions {
  t: Translate;
  onEdit: (profile: AgentOrchestrationProfileRead) => void;
  onToggle: (profile: AgentOrchestrationProfileRead) => void;
}

/** 构造编排配置列表的列。 */
export function buildOrchestrationProfileColumns({
  t,
  onEdit,
  onToggle,
}: OrchestrationProfileColumnsOptions): TableColumnsType<AgentOrchestrationProfileRead> {
  return [
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
        <EnabledCell record={record} t={t} onToggle={onToggle} />
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
        <ActionsCell record={record} t={t} onEdit={onEdit} />
      ),
    },
  ];
}
