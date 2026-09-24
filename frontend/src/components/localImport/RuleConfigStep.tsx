import { useMemo, useState } from "react";
import { Button, Checkbox, Input, Space, Switch, Table, Tag, Typography } from "antd";
import type { TableColumnsType, TableProps } from "antd";
import { useTranslation } from "../../i18n";
import type { TableSchema } from "../../types/datasource";
import type { ImportRuleConfig } from "../../types/localImport";
import {
  allColumnNames,
  chosenColumns,
  DEFAULT_JOIN_INFERENCE,
  tableNameIndex,
  toRulesWithJoinInference,
  type ColumnSubset,
  type JoinInferenceFlags,
} from "./importSelection";

const { Text } = Typography;

interface RuleConfigStepProps {
  tables: readonly TableSchema[];
  rules: ImportRuleConfig;
  selectedTables: string[];
  columnSubset: ColumnSubset;
  onRulesChange: (rules: ImportRuleConfig) => void;
  onSelectedTablesChange: (tables: string[]) => void;
  onColumnSubsetChange: (subset: ColumnSubset) => void;
}

// 本地导入「规则配置」：选表（多选）+ 单选一张表时做部分列选 + 关联推断开关。
// 表格选与列选都是纯状态回调，真正的白名单收敛逻辑在 importSelection（可单测）。
export default function RuleConfigStep({
  tables,
  rules,
  selectedTables,
  columnSubset,
  onRulesChange,
  onSelectedTablesChange,
  onColumnSubsetChange,
}: RuleConfigStepProps) {
  const { t } = useTranslation();
  const [searchText, setSearchText] = useState("");

  const index = useMemo(() => tableNameIndex(tables), [tables]);

  const filteredTables = useMemo(() => {
    const query = searchText.trim().toLowerCase();
    if (!query) return tables;
    return tables.filter((tb) => tb.tableName.toLowerCase().includes(query));
  }, [tables, searchText]);

  const singleTableName = selectedTables.length === 1 ? selectedTables[0] : null;
  const singleFullColumns = singleTableName
    ? allColumnNames(index.get(singleTableName))
    : [];
  const singleChosen = singleTableName
    ? chosenColumns(columnSubset, singleTableName, singleFullColumns)
    : [];

  const flags: JoinInferenceFlags = {
    inferDeclaredFk:
      rules.joinInference?.inferDeclaredFk ?? DEFAULT_JOIN_INFERENCE.inferDeclaredFk,
    inferNameConvention:
      rules.joinInference?.inferNameConvention ??
      DEFAULT_JOIN_INFERENCE.inferNameConvention,
  };

  const handleJoinToggle = (key: keyof JoinInferenceFlags, value: boolean) => {
    onRulesChange(toRulesWithJoinInference(rules, { ...flags, [key]: value }));
  };

  const handleSelectFiltered = () => {
    const next = new Set(selectedTables);
    filteredTables.forEach((tb) => next.add(tb.tableName));
    onSelectedTablesChange(Array.from(next));
  };

  // selections 下拉跨页作用于完整 dataSource（过滤后的全部表），而非表头 checkbox
  // 默认的当前页——表多时分页后仍能一键全选。首项复用工具栏「全选当前筛选」的
  // 累加语义（并入已选，不清掉此前跨页勾选的表），与 antd 内置 SELECTION_ALL 的
  // 「替换为当前筛选集」区分，避免分批选表时静默丢失已选。
  const rowSelection: TableProps<TableSchema>["rowSelection"] = {
    selectedRowKeys: selectedTables,
    onChange: (keys) => onSelectedTablesChange(keys.map(String)),
    preserveSelectedRowKeys: true,
    selections: [
      {
        key: "select-all-filtered",
        text: t("localImport.config.selectFiltered"),
        onSelect: () => handleSelectFiltered(),
      },
      Table.SELECTION_INVERT,
      Table.SELECTION_NONE,
    ],
  };

  const tableColumns: TableColumnsType<TableSchema> = [
    { title: t("localImport.config.tableName"), dataIndex: "tableName" },
    {
      title: t("localImport.config.columnCount"),
      dataIndex: "columns",
      width: 110,
      render: (columns: TableSchema["columns"]) => columns.length,
    },
  ];

  const handleColumnsChange = (chosen: string[]) => {
    if (!singleTableName) return;
    // 收窄结果原样入 subset（含全列/空列）；pruneColumnSubset 在发送层收敛为
    // 「真子集才下发」，此处无需判断。
    onColumnSubsetChange({ ...columnSubset, [singleTableName]: chosen });
  };

  const renderColumnPicker = () => {
    if (selectedTables.length === 0) {
      return <Text type="secondary">{t("localImport.config.noTableHint")}</Text>;
    }
    if (selectedTables.length > 1) {
      return <Text type="secondary">{t("localImport.config.multiModeHint")}</Text>;
    }
    return (
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Text strong>
          {t("localImport.config.singleModeTitle", { table: singleTableName ?? "" })}
        </Text>
        <div
          style={{
            maxHeight: 220,
            overflow: "auto",
            border: "1px solid #f0f0f0",
            borderRadius: 6,
            padding: "8px 12px",
          }}
        >
          <Checkbox.Group
            style={{ width: "100%" }}
            value={singleChosen}
            onChange={(vals) => handleColumnsChange(vals.map(String))}
          >
            <Space direction="vertical" size={2} style={{ width: "100%" }}>
              {singleFullColumns.map((col) => (
                <Checkbox key={col} value={col}>
                  {col}
                </Checkbox>
              ))}
            </Space>
          </Checkbox.Group>
        </div>
        <Text type="secondary">
          {t("localImport.config.columnChosenCount", {
            chosen: singleChosen.length,
            total: singleFullColumns.length,
          })}
        </Text>
      </Space>
    );
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Space wrap>
        <Text strong>{t("localImport.config.selectTablesTitle")}</Text>
        <Input
          allowClear
          placeholder={t("localImport.config.searchPlaceholder")}
          style={{ width: 240 }}
          onChange={(e) => setSearchText(e.target.value)}
        />
        <Button onClick={handleSelectFiltered}>
          {t("localImport.config.selectFiltered")}
        </Button>
        <Text type="secondary">
          {t("localImport.config.selectedCount", {
            count: selectedTables.length,
            total: tables.length,
          })}
        </Text>
      </Space>

      <div data-testid="ruleTable">
        <Table<TableSchema>
          rowKey="tableName"
          dataSource={filteredTables}
          columns={tableColumns}
          rowSelection={rowSelection}
          // defaultPageSize 仅作初始值；传受控 pageSize 会让 size changer 的改动每次被 props 压回（no-op）。
          pagination={{
            defaultPageSize: 50,
            showSizeChanger: true,
            pageSizeOptions: [20, 50, 100, 200, 500],
          }}
          size="small"
        />
      </div>

      <Space wrap align="center">
        <Text strong>{t("localImport.config.joinInferenceTitle")}</Text>
        <Switch
          size="small"
          checked={flags.inferDeclaredFk}
          onChange={(v) => handleJoinToggle("inferDeclaredFk", v)}
        />
        <Tag color="blue">{t("localImport.config.inferDeclaredFk")}</Tag>
        <Switch
          size="small"
          checked={flags.inferNameConvention}
          onChange={(v) => handleJoinToggle("inferNameConvention", v)}
        />
        <Tag color="purple">{t("localImport.config.inferNameConvention")}</Tag>
      </Space>

      {renderColumnPicker()}
    </Space>
  );
}
