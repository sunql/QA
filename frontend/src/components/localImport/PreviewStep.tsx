import { Table, Button, Space } from "antd";
import type { TableColumnsType } from "antd";
import { useTranslation } from "../../i18n";
import type {
  ImportExecuteRequest,
  ImportPreviewResponse,
  ProposedClass,
} from "../../types/localImport";

interface PreviewStepProps {
  preview: ImportPreviewResponse;
  onExecute: (request: ImportExecuteRequest) => void;
}

// 预览确认：只读展示 proposedClasses 摘要，确认后构造 ImportExecuteRequest 提交导入。
export default function PreviewStep({ preview, onExecute }: PreviewStepProps) {
  const { t } = useTranslation();

  const columns: TableColumnsType<ProposedClass> = [
    { title: t("localImport.preview.sourceTable"), dataIndex: "sourceTable" },
    { title: t("localImport.preview.className"), dataIndex: "className" },
    {
      title: t("localImport.preview.propertyCount"),
      key: "propertyCount",
      render: (_, record) => record.properties.length,
    },
  ];

  const handleConfirm = () => {
    onExecute({
      confirmedClasses: preview.proposedClasses,
      confirmedJoins: preview.proposedJoins,
      conflictResolutions: [],
      syncEmbeddings: false,
    });
  };

  return (
    <Space direction="vertical" size="large" style={{ width: "100%" }}>
      <Table
        rowKey="sourceTable"
        dataSource={preview.proposedClasses}
        columns={columns}
        pagination={false}
        size="small"
      />
      <div style={{ textAlign: "right" }}>
        <Button type="primary" onClick={handleConfirm}>
          {t("localImport.confirmImport")}
        </Button>
      </div>
    </Space>
  );
}
