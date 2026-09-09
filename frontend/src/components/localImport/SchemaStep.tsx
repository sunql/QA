import { Select, Space, Typography } from "antd";
import { useTranslation } from "../../i18n";

const { Text } = Typography;

interface SchemaStepProps {
  // 数据源可选 schema（Oracle owner）；PG/MySQL 为空时本步不渲染，由向导直接跳到选表。
  schemas: readonly string[];
  // 当前已选 schema（owner）；null = 未选。
  selected: string | null;
  // 正在加载该 schema 下的表：禁用切换，避免竞态。
  disabled?: boolean;
  onSelect: (schema: string) => void;
}

// 本地导入向导「选择 Schema」步：列出 Oracle owner 命名空间，选择后按该 owner 加载表。
export default function SchemaStep({
  schemas,
  selected,
  disabled = false,
  onSelect,
}: SchemaStepProps) {
  const { t } = useTranslation();

  return (
    <Space direction="vertical" size="small">
      <Space align="center" wrap>
        <Text strong>{t("localImport.schema.selectLabel")}</Text>
        <Select
          data-testid="schemaSelect"
          style={{ width: 280 }}
          placeholder={t("localImport.schema.placeholder")}
          value={selected}
          disabled={disabled}
          onChange={(value: string) => onSelect(value)}
          options={schemas.map((owner) => ({ value: owner, label: owner }))}
          showSearch
          optionFilterProp="label"
        />
        <Text type="secondary">
          {t("localImport.schema.ownerCount", { count: schemas.length })}
        </Text>
      </Space>
      <Text type="secondary">
        {t("localImport.schema.hint")}
      </Text>
    </Space>
  );
}
