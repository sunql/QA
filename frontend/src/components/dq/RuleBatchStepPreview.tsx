/** 批量新建规则 — 步骤 3:预览确认（feat-rule-batch-create，2026-09-15）
 *
 * 全局默认值（阈值 / 严重级别 / 启用 / 责任方 / 描述）+ 逐条规则可编辑预览。
 * 冲突规则名标红 + 失败行标 saveError。
 */

import {
  Alert,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";

import type { BatchDraftRule } from "../../pages/DataQualityRuleBatchCreatePage";

const { Text } = Typography;

export interface RuleBatchStepPreviewProps {
  drafts: BatchDraftRule[];
  duplicateIndices: number[];
  className: string;
  targetTable: string;
  globalOwner: string;
  globalDescription: string;
  globalSeverity: string;
  globalThreshold: string;
  globalEnabled: boolean;
  onChangeGlobalOwner: (v: string) => void;
  onChangeGlobalDescription: (v: string) => void;
  onChangeGlobalSeverity: (v: string) => void;
  onChangeGlobalThreshold: (v: string) => void;
  onChangeGlobalEnabled: (v: boolean) => void;
  onChangeDrafts: (drafts: BatchDraftRule[]) => void;
}

export function RuleBatchStepPreview(
  props: RuleBatchStepPreviewProps,
): JSX.Element {
  const { t } = useTranslation();

  function patchDraft(idx: number, patch: Partial<BatchDraftRule>): void {
    props.onChangeDrafts(
      props.drafts.map((d, i) => (i === idx ? { ...d, ...patch } : d)),
    );
  }

  const dupSet = new Set(props.duplicateIndices);

  const columnsDef: ColumnsType<BatchDraftRule> = [
    {
      title: t("dataQuality.ruleCode"),
      dataIndex: "ruleCode",
      key: "ruleCode",
      width: 240,
      render: (_: unknown, row, idx) =>
        dupSet.has(idx) ? (
          <Tag color="red">{t("dataQuality.batchCreate.duplicateName")}</Tag>
        ) : (
          <Text code>{row.ruleCode}</Text>
        ),
    },
    {
      title: t("dataQuality.ruleName"),
      dataIndex: "ruleName",
      key: "ruleName",
      width: 280,
      render: (_: unknown, row, idx) => (
        <Input
          value={row.ruleName}
          onChange={(e) =>
            patchDraft(idx, {
              ruleName: e.target.value,
              isAutoName: false,
            })
          }
          status={dupSet.has(idx) ? "error" : undefined}
        />
      ),
    },
    {
      title: t("dataQuality.ruleType"),
      dataIndex: "ruleType",
      key: "ruleType",
      width: 120,
      render: (_: unknown, row, idx) => (
        <Select
          value={row.ruleType}
          onChange={(v) => patchDraft(idx, { ruleType: v })}
          options={["COMPLETENESS", "VALIDITY", "UNIQUENESS", "CONSISTENCY", "REFERENTIAL", "TIMELINESS"].map((rt) => ({
            value: rt,
            label: t(`dataQuality.ruleTypeLabels.${rt}`, { defaultValue: rt }),
          }))}
        />
      ),
    },
    {
      title: t("dataQuality.targetColumn"),
      dataIndex: "targetColumn",
      key: "targetColumn",
      width: 140,
    },
    {
      title: t("dataQuality.ruleExpression"),
      dataIndex: "ruleExpression",
      key: "ruleExpression",
      width: 240,
      render: (_: unknown, row, idx) => (
        <Input
          value={row.ruleExpression ?? ""}
          onChange={(e) =>
            patchDraft(idx, { ruleExpression: e.target.value || null })
          }
        />
      ),
    },
    {
      title: t("dataQuality.severity"),
      dataIndex: "severity",
      key: "severity",
      width: 110,
      render: (_: unknown, row, idx) => (
        <Select
          value={row.severity}
          onChange={(v) => patchDraft(idx, { severity: v })}
          options={["HIGH", "MEDIUM", "LOW", "INFO"].map((s) => ({
            value: s,
            label: t(`dataQuality.severityLabels.${s}`, { defaultValue: s }),
          }))}
        />
      ),
    },
    {
      title: t("dataQuality.threshold"),
      dataIndex: "threshold",
      key: "threshold",
      width: 110,
      render: (_: unknown, row, idx) => (
        <InputNumber
          value={parseFloat(row.threshold)}
          min={0}
          max={100}
          step={0.01}
          onChange={(v) =>
            patchDraft(idx, {
              threshold: v !== null ? String(v) : "0",
            })
          }
          style={{ width: "100%" }}
        />
      ),
    },
    {
      title: t("dataQuality.enabled"),
      dataIndex: "isEnabled",
      key: "isEnabled",
      width: 90,
      render: (_: unknown, row, idx) => (
        <Switch
          checked={row.isEnabled}
          onChange={(v) => patchDraft(idx, { isEnabled: v })}
        />
      ),
    },
  ];

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Alert
        type="info"
        message={t("dataQuality.batchCreate.previewTitle", {
          count: props.drafts.length,
        })}
      />

      <Form layout="inline">
        <Form.Item label={t("dataQuality.batchCreate.globalThreshold")}>
          <InputNumber
            value={parseFloat(props.globalThreshold)}
            min={0}
            max={100}
            step={0.01}
            onChange={(v) =>
              props.onChangeGlobalThreshold(v !== null ? String(v) : "0")
            }
          />
        </Form.Item>
        <Form.Item label={t("dataQuality.batchCreate.globalSeverity")}>
          <Select
            value={props.globalSeverity}
            onChange={props.onChangeGlobalSeverity}
            options={["HIGH", "MEDIUM", "LOW", "INFO"].map((s) => ({
              value: s,
              label: t(`dataQuality.severityLabels.${s}`, { defaultValue: s }),
            }))}
            style={{ width: 120 }}
          />
        </Form.Item>
        <Form.Item label={t("dataQuality.batchCreate.globalEnabled")}>
          <Switch
            checked={props.globalEnabled}
            onChange={props.onChangeGlobalEnabled}
          />
        </Form.Item>
        <Form.Item
          label={t("dataQuality.batchCreate.globalOwner")}
          required
          validateStatus={!props.globalOwner.trim() ? "warning" : undefined}
        >
          <Input
            value={props.globalOwner}
            onChange={(e) => props.onChangeGlobalOwner(e.target.value)}
            style={{ width: 160 }}
          />
        </Form.Item>
        <Form.Item label={t("dataQuality.batchCreate.globalDescription")}>
          <Input
            value={props.globalDescription}
            onChange={(e) => props.onChangeGlobalDescription(e.target.value)}
            style={{ width: 240 }}
          />
        </Form.Item>
      </Form>

      <Table<BatchDraftRule>
        rowKey="key"
        dataSource={props.drafts}
        columns={columnsDef}
        pagination={{ pageSize: 50, showSizeChanger: false }}
        size="small"
        scroll={{ x: 1400 }}
      />
    </Space>
  );
}