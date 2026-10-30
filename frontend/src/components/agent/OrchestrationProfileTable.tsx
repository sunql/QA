import { Table } from "antd";
import { useTranslation } from "../../i18n";
import { buildOrchestrationProfileColumns } from "./OrchestrationProfileColumns";
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
  const columns = buildOrchestrationProfileColumns({ t, onEdit, onToggle });
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
