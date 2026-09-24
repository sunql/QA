import { useMemo, useState } from "react";
import { Button, Input, Space, Table, Tag, Typography } from "antd";
import type { TableColumnsType, TableProps } from "antd";
import { useTranslation } from "../../i18n";
import type {
  ImportExecuteRequest,
  ImportPreviewResponse,
  ProposedClass,
  ProposedJoin,
} from "../../types/localImport";
import { buildExecuteRequest, joinKey, validJoins } from "./importSelection";

const { Text } = Typography;

interface PreviewStepProps {
  preview: ImportPreviewResponse;
  onExecute: (request: ImportExecuteRequest) => void;
}

// 预览确认：分页展示 proposedClasses，支持按表名/类名搜索、勾选子集分批导入。
// 关联关系单独一节：每条 join 一个勾选框，两端都被勾选的类才可选；可整表开关。
// 已选类对应 join 默认全部勾上，落库前由 buildExecuteRequest 收敛为「两端都选且勾选」的边。
export default function PreviewStep({ preview, onExecute }: PreviewStepProps) {
  const { t } = useTranslation();
  const [searchText, setSearchText] = useState("");
  const [selectedRowKeys, setSelectedRowKeys] = useState<string[]>([]);
  // 被用户手动关掉的 join（joinKey 集合）。有效集合 = 两端已选 join − 此排除集。
  const [excludedJoinKeys, setExcludedJoinKeys] = useState<ReadonlySet<string>>(
    () => new Set<string>()
  );

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
  // selections 下拉跨页作用于完整 dataSource（过滤后的全部类）。首项复用工具栏
  // 「全选当前筛选」的累加语义：分批导入时并入当前筛选类，而不像 antd 内置
  // SELECTION_ALL 那样「替换为当前筛选集」——否则会静默丢掉此前批次勾选的类。
  const rowSelection: TableProps<ProposedClass>["rowSelection"] = {
    selectedRowKeys,
    onChange: (keys) => setSelectedRowKeys(keys.map(String)),
    preserveSelectedRowKeys: true,
    selections: [
      {
        key: "select-all-filtered",
        text: t("localImport.preview.selectFiltered"),
        onSelect: () => handleSelectFiltered(),
      },
      Table.SELECTION_INVERT,
      Table.SELECTION_NONE,
    ],
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

  const selectedClassSet = useMemo(
    () => new Set(selectedRowKeys),
    [selectedRowKeys],
  );

  // 两端都在已选类内的 join（可勾选导入）。其余照常展示但禁用，说明缺端。
  const valid = useMemo(
    () => validJoins(preview.proposedJoins, selectedClassSet),
    [preview.proposedJoins, selectedClassSet],
  );

  // 受控勾选集：validKeys − excludedJoinKeys（排除集中已失效的键被自然忽略）。
  const checkedJoinKeys = useMemo(() => {
    const result: string[] = [];
    valid.forEach((j) => {
      const key = joinKey(j);
      if (!excludedJoinKeys.has(key)) result.push(key);
    });
    return result;
  }, [valid, excludedJoinKeys]);

  const isJoinDisabled = (j: ProposedJoin) =>
    !selectedClassSet.has(j.sourceTable) || !selectedClassSet.has(j.targetTable);

  const joinRowSelection: TableProps<ProposedJoin>["rowSelection"] = {
    selectedRowKeys: checkedJoinKeys,
    onChange: (keys) => {
      // 反推出本次「仍被排除」的键：所有 valid 键中不在新选中集合里的。
      const selected = new Set(keys.map(String));
      const nextExcluded = new Set<string>();
      valid.forEach((j) => {
        const key = joinKey(j);
        if (!selected.has(key)) nextExcluded.add(key);
      });
      setExcludedJoinKeys(nextExcluded);
    },
    getCheckboxProps: (j) => ({ disabled: isJoinDisabled(j) }),
  };

  const joinColumns: TableColumnsType<ProposedJoin> = [
    {
      title: t("localImport.preview.joinSource"),
      key: "source",
      render: (_, j) => (
        <Text code>
          {j.sourceTable}.{j.sourceColumns.join("+")}
        </Text>
      ),
    },
    {
      title: t("localImport.preview.joinTarget"),
      key: "target",
      render: (_, j) => (
        <Text code>
          {j.targetTable}.{j.targetColumns.join("+")}
        </Text>
      ),
    },
    {
      title: t("localImport.preview.joinType"),
      dataIndex: "joinType",
      width: 90,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: t("localImport.preview.relationType"),
      dataIndex: "relationType",
      width: 100,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: t("localImport.preview.inferredBy"),
      key: "inferredBy",
      width: 120,
      render: (_, j) => {
        if (j.inferredBy === "name_convention") {
          return <Tag color="purple">{t("localImport.preview.inferredByNameConvention")}</Tag>;
        }
        if (j.inferredBy === "declared_fk") {
          return <Tag color="blue">{t("localImport.preview.inferredByDeclaredFk")}</Tag>;
        }
        return <Text type="secondary">—</Text>;
      },
    },
  ];

  const handleConfirm = () => {
    // 最终启用集 = checkedJoinKeys（valid 中未被用户排除的键）。
    const effectiveKeys = new Set(checkedJoinKeys);
    onExecute(buildExecuteRequest(preview, selectedClassSet, effectiveKeys));
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
      <div data-testid="classTable">
        <Table
          rowKey="sourceTable"
          dataSource={filteredClasses}
          columns={columns}
          rowSelection={rowSelection}
          // defaultPageSize 仅作初始值；受控 pageSize 会让 size changer 改动被 props 压回（no-op）。
          pagination={{ defaultPageSize: 50, showSizeChanger: true }}
          size="small"
        />
      </div>

      {preview.proposedJoins.length > 0 && (
        <>
          <Space wrap>
            <Text strong>{t("localImport.preview.joinsTitle")}</Text>
            <Text type="secondary">
              {t("localImport.preview.joinCount", { count: checkedJoinKeys.length })}
            </Text>
          </Space>
          <div data-testid="joinTable">
            <Table<ProposedJoin>
              rowKey={joinKey}
              dataSource={preview.proposedJoins}
              columns={joinColumns}
              rowSelection={joinRowSelection}
              pagination={{ defaultPageSize: 50, showSizeChanger: true }}
              size="small"
            />
          </div>
        </>
      )}

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
