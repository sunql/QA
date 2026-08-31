import { useMemo, useState } from "react";
import { Button, Input, Space, Table } from "antd";
import type { TableColumnsType, TableProps } from "antd";
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

// 预览确认：分页展示 proposedClasses，支持按表名/类名搜索、勾选子集分批导入。
// 确认时只提交勾选的类；join 只保留两端都在勾选子集内的，避免对未导入的目标表生成悬空关联。
export default function PreviewStep({ preview, onExecute }: PreviewStepProps) {
  const { t } = useTranslation();
  const [searchText, setSearchText] = useState("");
  const [selectedRowKeys, setSelectedRowKeys] = useState<string[]>([]);

  // 搜索为展示层过滤：不改变选中集合，也不影响最终提交范围。
  const filteredClasses = useMemo(() => {
    const query = searchText.trim().toLowerCase();
    if (!query) return preview.proposedClasses;
    return preview.proposedClasses.filter(
      (c) =>
        c.sourceTable.toLowerCase().includes(query) ||
        c.className.toLowerCase().includes(query),
    );
  }, [preview.proposedClasses, searchText]);

  // preserveSelectedRowKeys：搜索/翻页移除当前页后，已勾选的行仍保留在选中集合内。
  const rowSelection: TableProps<ProposedClass>["rowSelection"] = {
    selectedRowKeys,
    onChange: (keys) => setSelectedRowKeys(keys.map(String)),
    preserveSelectedRowKeys: true,
  };

  const columns: TableColumnsType<ProposedClass> = [
    { title: t("localImport.preview.sourceTable"), dataIndex: "sourceTable" },
    { title: t("localImport.preview.className"), dataIndex: "className" },
    {
      title: t("localImport.preview.propertyCount"),
      key: "propertyCount",
      render: (_, record) => record.properties.length,
    },
  ];

  // 累计语义：把当前筛选命中的行并入已选集合（分批导入时按子集逐批勾选）。
  // 取消选中用表头 checkbox（对当前筛选集 toggle）。
  const handleSelectFiltered = () => {
    setSelectedRowKeys((prev) => {
      const next = new Set(prev);
      filteredClasses.forEach((c) => next.add(c.sourceTable));
      return Array.from(next);
    });
  };

  const handleConfirm = () => {
    const selectedTables = new Set(selectedRowKeys);
    onExecute({
      confirmedClasses: preview.proposedClasses
        .filter((c) => selectedTables.has(c.sourceTable))
        .map((c) => ({ ...c, isSelected: true })),
      confirmedJoins: preview.proposedJoins
        .filter(
          (j) =>
            selectedTables.has(j.sourceTable) && selectedTables.has(j.targetTable),
        )
        .map((j) => ({ ...j, isSelected: true })),
      conflictResolutions: [],
      syncEmbeddings: false,
    });
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Space>
        <Input
          allowClear
          placeholder={t("localImport.preview.searchPlaceholder")}
          style={{ width: 260 }}
          onChange={(e) => setSearchText(e.target.value)}
        />
        <Button onClick={handleSelectFiltered}>
          {t("localImport.preview.selectFiltered")}
        </Button>
        <span>
          {t("localImport.preview.selectedCount", {
            count: selectedRowKeys.length,
            total: preview.proposedClasses.length,
          })}
        </span>
      </Space>
      <Table
        rowKey="sourceTable"
        dataSource={filteredClasses}
        columns={columns}
        rowSelection={rowSelection}
        pagination={{ pageSize: 50, showSizeChanger: true }}
        size="small"
      />
      <div style={{ textAlign: "right" }}>
        <Button
          type="primary"
          onClick={handleConfirm}
          disabled={selectedRowKeys.length === 0}
        >
          {t("localImport.confirmImport")}
        </Button>
      </div>
    </Space>
  );
}
