/** 批量新建规则 — 步骤 2:列与规则（feat-rule-batch-create，2026-09-15）
 *
 * 列清单表格：勾选 + 规则类型 + 自动模板 + 表达式（可改）。
 * 切换规则类型时重新跑模板；用户改表达式后置 isAutoExpression=false。
 */

import { useEffect } from "react";
import { Alert, Checkbox, Input, Select, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";

import { suggestRuleExpression } from "../../utils/ruleExpressionTemplates";

export interface ColumnWithRule {
  columnName: string;
  dataType: string;
  isPrimaryKey: boolean;
  selected: boolean;
  ruleType: string;
  templateId: string | null;
  expression: string | null;
  isAutoExpression: boolean;
  /** 无自动模板（需人工填） */
  noTemplate: boolean;
}

export interface RuleBatchStepColumnsProps {
  columns: ColumnWithRule[];
  onChangeColumns: (cols: ColumnWithRule[]) => void;
  tableName: string;
}

const RULE_TYPES = [
  { value: "COMPLETENESS", labelKey: "COMPLETENESS" },
  { value: "VALIDITY", labelKey: "VALIDITY" },
  { value: "UNIQUENESS", labelKey: "UNIQUENESS" },
  { value: "CONSISTENCY", labelKey: "CONSISTENCY" },
  { value: "REFERENTIAL", labelKey: "REFERENTIAL" },
  { value: "TIMELINESS", labelKey: "TIMELINESS" },
];

export function RuleBatchStepColumns(
  props: RuleBatchStepColumnsProps,
): JSX.Element {
  const { t } = useTranslation();
  const selectedCount = props.columns.filter((c) => c.selected).length;

  // 切换规则类型或列变化时重新算表达式（仅在 isAutoExpression=true 时刷新）
  useEffect(() => {
    const next = props.columns.map((c) => {
      if (!c.selected || !c.isAutoExpression) return c;
      const r = suggestRuleExpression({
        column: { name: c.columnName, dataType: c.dataType },
        ruleType: c.ruleType as
          | "COMPLETENESS"
          | "VALIDITY"
          | "UNIQUENESS"
          | "CONSISTENCY"
          | "REFERENTIAL"
          | "TIMELINESS",
      });
      return {
        ...c,
        templateId: r.templateId,
        expression: r.expression,
        noTemplate: !r.canAutoFill,
        isAutoExpression: r.canAutoFill,
      };
    });
    const changed =
      JSON.stringify(next.map((c) => ({ s: c.selected, e: c.expression, t: c.templateId }))) !==
      JSON.stringify(props.columns.map((c) => ({ s: c.selected, e: c.expression, t: c.templateId })));
    if (changed) props.onChangeColumns(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.columns.map((c) => c.selected + "|" + c.ruleType).join(",")]);

  function patchColumn(idx: number, patch: Partial<ColumnWithRule>): void {
    props.onChangeColumns(
      props.columns.map((c, i) => (i === idx ? { ...c, ...patch } : c)),
    );
  }

  const columnsDef: ColumnsType<ColumnWithRule> = [
    {
      title: (
        <Checkbox
          checked={
            selectedCount > 0 && selectedCount === props.columns.length
          }
          indeterminate={
            selectedCount > 0 && selectedCount < props.columns.length
          }
          onChange={(e) =>
            props.onChangeColumns(
              props.columns.map((c) => ({
                ...c,
                selected: e.target.checked,
              })),
            )
          }
        />
      ),
      dataIndex: "selected",
      key: "selected",
      width: 56,
      render: (_: unknown, row, idx) => (
        <Checkbox
          checked={row.selected}
          onChange={(e) =>
            patchColumn(idx, { selected: e.target.checked })
          }
        />
      ),
    },
    {
      title: t("dataQuality.targetColumn"),
      dataIndex: "columnName",
      key: "columnName",
      width: 200,
    },
    {
      title: t("dataQuality.targetTable"),
      dataIndex: "dataType",
      key: "dataType",
      width: 160,
    },
    {
      title: t("dataQuality.ruleType"),
      dataIndex: "ruleType",
      key: "ruleType",
      width: 160,
      render: (_: unknown, row, idx) => (
        <Select
          value={row.ruleType}
          onChange={(v) => patchColumn(idx, { ruleType: v, isAutoExpression: true })}
          options={RULE_TYPES.map((rt) => ({
            value: rt.value,
            label: t(`dataQuality.ruleTypeLabels.${rt.labelKey}`, {
              defaultValue: rt.value,
            }),
          }))}
          style={{ width: "100%" }}
        />
      ),
    },
    {
      title: t("dataQuality.ruleExpression"),
      dataIndex: "expression",
      key: "expression",
      render: (_: unknown, row, idx) => {
        if (!row.selected) return <span style={{ color: "#bbb" }}>—</span>;
        if (row.noTemplate) {
          return (
            <Tooltip title={t("dataQuality.batchCreate.noTemplateHint")}>
              <Input
                value={row.expression ?? ""}
                placeholder={t("dataQuality.batchCreate.noTemplateHint")}
                onChange={(e) =>
                  patchColumn(idx, {
                    expression: e.target.value || null,
                    isAutoExpression: false,
                    noTemplate: false,
                  })
                }
              />
            </Tooltip>
          );
        }
        return (
          <Input
            value={row.expression ?? ""}
            onChange={(e) =>
              patchColumn(idx, {
                expression: e.target.value || null,
                isAutoExpression: false,
              })
            }
            addonAfter={
              row.isAutoExpression ? (
                <Tag color="processing">
                  {t("dataQuality.batchCreate.autoBadge")}
                </Tag>
              ) : (
                <Tag>
                  {t("dataQuality.batchCreate.customBadge")}
                </Tag>
              )
            }
          />
        );
      },
    },
  ];

  return (
    <>
      <Alert
        type="info"
        message={t("dataQuality.batchCreate.selectColumns", {
          total: props.columns.length,
          selected: selectedCount,
        })}
        style={{ marginBottom: 12 }}
      />
      <Table<ColumnWithRule>
        rowKey="columnName"
        dataSource={props.columns}
        columns={columnsDef}
        pagination={false}
        size="small"
        title={() => (
          <span>
            {props.tableName} —{" "}
            <span style={{ color: "#999" }}>
              {t("dataQuality.targetTable")}
            </span>
          </span>
        )}
      />
    </>
  );
}