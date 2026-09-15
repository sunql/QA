/** 批量新建规则 — 步骤 1:基础配置（feat-rule-batch-create，2026-09-15）
 *
 * 数据源 + 本体类 + 目标表（fuzzy）。选类后自动填 source_table，可手动改。
 */

import { Form, Input, Select, Spin } from "antd";
import { useTranslation } from "react-i18next";

import type { DataSource } from "../../types/datasource";
import type { OntologyClass } from "../../types/ontology";

export interface RuleBatchStepBasicProps {
  datasources: DataSource[];
  classes: OntologyClass[];
  datasourceId: number | null;
  classId: number | null;
  targetTable: string;
  tableQuery: string;
  filteredTables: { tableName: string; columns: unknown[] }[];
  loadingSchema: boolean;
  schemaCount: number;
  onChangeDatasource: (id: number | null) => void;
  onChangeClass: (id: number | null) => void;
  onChangeTargetTable: (table: string) => void;
  onChangeTableQuery: (q: string) => void;
}

export function RuleBatchStepBasic(props: RuleBatchStepBasicProps): JSX.Element {
  const { t } = useTranslation();

  return (
    <Form layout="vertical">
      <Form.Item label={t("dataQuality.datasource")} required>
        <Select
          placeholder={t("dataQuality.batchCreate.datasourcePlaceholder")}
          value={props.datasourceId ?? undefined}
          onChange={props.onChangeDatasource}
          options={(props.datasources ?? []).map((d) => ({
            value: d.id,
            label: `${d.name}${d.type ? ` (${d.type})` : ""}`,
          }))}
          showSearch
          optionFilterProp="label"
        />
      </Form.Item>

      <Form.Item label={t("dataQuality.filterClassName")} required>
        <Select
          placeholder={t("dataQuality.batchCreate.classPlaceholder")}
          value={props.classId ?? undefined}
          onChange={props.onChangeClass}
          options={(props.classes ?? []).map((c) => ({
            value: c.id,
            label: c.sourceTable
              ? `${c.className} (${c.sourceTable})`
              : c.className,
          }))}
          showSearch
          optionFilterProp="label"
          disabled={props.datasourceId == null}
        />
      </Form.Item>

      <Form.Item
        label={t("dataQuality.targetTable")}
        required
        extra={
          props.loadingSchema
            ? t("dataQuality.batchCreate.loadingSchema")
            : props.schemaCount > 0
              ? t("dataQuality.batchCreate.loadedSchema", {
                  count: props.schemaCount,
                })
              : null
        }
      >
        <Spin spinning={props.loadingSchema}>
          <Select
            placeholder={t("dataQuality.batchCreate.tablePlaceholder")}
            value={props.targetTable || undefined}
            onChange={props.onChangeTargetTable}
            showSearch
            searchValue={props.tableQuery}
            onSearch={props.onChangeTableQuery}
            filterOption={false}
            options={(props.filteredTables ?? []).map((t2) => ({
              value: t2.tableName,
              label: t2.tableName,
            }))}
            disabled={props.datasourceId == null}
            notFoundContent={null}
          />
        </Spin>
      </Form.Item>

      {/* 保留旧的 Input，隐藏在 Form 内部用于非 fuzzy 兜底；目前走 fuzzy 选项。 */}
      <Input type="hidden" value={props.targetTable} />
    </Form>
  );
}