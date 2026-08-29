/** 通用多条件筛选条：用于本体管理页各 Tab（类/属性/指标/关联）。
 *
 * 约定（与 src/utils/ontologyFilter.ts 对齐）：
 * - 每个字段的值统一为字符串；Select 的清除/重选会把值归一为 ""（视为无条件）。
 * - label 同时作为 placeholder；Select 带 aria-label 会与编辑弹窗同名下拉
 *   的 Form.Item label 冲突（getByRole name 查询会命中多个），故不加。
 */
import { Input, Select, Space, Button } from "antd";
import { ClearOutlined } from "@ant-design/icons";
import { useTranslation } from "../../i18n";
import type { FilterValues } from "../../utils/ontologyFilter";

export interface FilterField {
  key: string;
  label: string;
  type?: "input" | "select";
  /** select 专用：下拉选项。 */
  options?: { value: string; label: string }[];
  /** 控件宽度（px）；缺省 input 180 / select 200。 */
  width?: number;
}

export interface FilterBarProps {
  fields: FilterField[];
  values: FilterValues;
  onChange: (key: string, value: string) => void;
  onReset: () => void;
}

export default function FilterBar({ fields, values, onChange, onReset }: FilterBarProps) {
  const { t } = useTranslation();
  const hasActiveFilter = fields.some((f) => (values[f.key] ?? "").length > 0);

  return (
    <Space wrap style={{ marginBottom: 16 }}>
      {fields.map((field) => {
        const width = field.width ?? (field.type === "select" ? 200 : 180);
        if (field.type === "select") {
          return (
            <Select
              key={field.key}
              allowClear
              placeholder={field.label}
              style={{ width }}
              options={field.options}
              value={values[field.key] ? values[field.key] : undefined}
              onChange={(v) => onChange(field.key, v === undefined ? "" : String(v))}
            />
          );
        }
        return (
          <Input
            key={field.key}
            allowClear
            placeholder={field.label}
            style={{ width }}
            value={values[field.key] ?? ""}
            onChange={(e) => onChange(field.key, e.target.value)}
          />
        );
      })}
      <Button icon={<ClearOutlined />} disabled={!hasActiveFilter} onClick={onReset}>
        {t("forms.ontology.filter.reset")}
      </Button>
    </Space>
  );
}
