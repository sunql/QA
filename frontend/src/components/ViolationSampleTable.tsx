/** 数据质量评估报告 — 违规样本明细表（feat-dq-evaluation-report，Phase 5）。
 *
 * 复用组件：详情页 accordion 里嵌入此组件；Phase 8 对比页也可能复用。
 * 列：rule_id / target_table / target_column / sample_pk_values / sample_size / captured_at。
 * 列内容用 antd `Table` + `useMemo` 包列定义（参考 `frontend/src/pages/DataQualityPage.tsx`）。
 *
 * feat-violation-sample-display (2026-09-15)：补「样本 PK 值」列（默认 20 条 / 规则）。
 * 历史 bug：之前只渲染元信息（ruleId / targetTable / totalViolations / sampleSize / capturedAt），
 * `samplePkValues` 这个 JSONB 字段完全不展示——用户看到「sampleSize=20」但不知道到底是哪 20 条，
 * 与「违规样本」表名完全不符。后端采样默认 20 条（DEFAULT_SAMPLE_LIMIT=20），数据全在
 * `sample_pk_values=[{"pk": "..."}, ...]` 里。
 */

import { useMemo } from "react";
import { Empty, Space, Table, Tag, Tooltip, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { ViolationSampleRead } from "../types/evaluationReport";
import { useTranslation } from "../i18n";

interface ViolationSampleTableProps {
  samples: ViolationSampleRead[];
  loading?: boolean;
}

/** 提取 sample_pk_values 里的 PK 字符串列表。
 *  feat-sample-pk-shape (2026-09-15)：兼容两种后端存储形态：
 *    新形态：entry = {"pk": "P001"} —— 直接取 pk
 *    老形态：entry = {"pk": {"row_id": "P001", "target_column": "...", "target_table": "..."}}
 *            —— 取 pk.row_id（sampler 旧返回 dict，sampleForRule 老版本没拆出来）
 *  容错：value 为 null/undefined 或空字符串时降级为「—」占位。 */
function extractPkValues(pks: ViolationSampleRead["samplePkValues"]): string[] {
  if (!Array.isArray(pks)) return [];
  return pks.map((entry) => {
    if (!entry || typeof entry !== "object" || !("pk" in entry)) return "—";
    const v = (entry as { pk: unknown }).pk;
    if (v === null || v === undefined || v === "") return "—";
    if (typeof v === "object" && v !== null && "row_id" in v) {
      // 老形态：从 sampler dict 里挖 row_id
      const rowId = (v as { row_id: unknown }).row_id;
      if (rowId === null || rowId === undefined || rowId === "") return "—";
      return String(rowId);
    }
    return String(v);
  });
}

export default function ViolationSampleTable({
  samples,
  loading,
}: ViolationSampleTableProps) {
  const { t } = useTranslation();

  const columns: ColumnsType<ViolationSampleRead> = useMemo(
    () => [
      {
        title: t("dataQuality.reports.detail.samplesTable.ruleId"),
        dataIndex: "ruleId",
        key: "ruleId",
        width: 90,
      },
      {
        title: t("dataQuality.reports.detail.samplesTable.targetTable"),
        dataIndex: "targetTable",
        key: "targetTable",
        width: 130,
      },
      {
        title: t("dataQuality.reports.detail.samplesTable.targetColumn"),
        dataIndex: "targetColumn",
        key: "targetColumn",
        width: 130,
        render: (v: string | null) => (v == null ? <Tag>—</Tag> : v),
      },
      {
        title: t("dataQuality.reports.detail.samplesTable.totalViolations"),
        dataIndex: "totalViolations",
        key: "totalViolations",
        width: 120,
        render: (v: number) => v.toLocaleString(),
      },
      // feat-sampling-error-visible (2026-09-15)：采样失败的行优先显示异常文本。
      // 失败 = dispatcher.collectSamples 抛错被 sampleForRule 落库 sampling_error；
      // 成功 = 显示 samplePkValues 的 PK Tag 列表（同 feat-violation-sample-display）。
      {
        title: t("dataQuality.reports.detail.samplesTable.samplePkValues"),
        key: "samplePkValues",
        width: 280,
        render: (_: unknown, record: ViolationSampleRead) => {
          if (record.samplingError) {
            // 失败优先：让用户立刻知道是 SQL/适配器问题，不是「真没数据」
            return (
              <Tooltip
                title={record.samplingError}
                placement="topLeft"
                color="red"
              >
                <Tag color="red" icon={<span>⚠</span>}>
                  {t(
                    "dataQuality.reports.detail.samplesTable.samplingFailed",
                  )}
                </Tag>
              </Tooltip>
            );
          }
          const pks = extractPkValues(record.samplePkValues);
          if (pks.length === 0) return <Tag>—</Tag>;
          const PREVIEW = 5;
          const visible = pks.slice(0, PREVIEW);
          const more = pks.length - visible.length;
          const tagNodes = visible.map((pk, idx) => (
            <Tag key={`${record.id}-${idx}`} color="red">
              {pk}
            </Tag>
          ));
          if (more > 0) {
            const fullList = pks.join(", ");
            return (
              <Tooltip title={fullList} placement="topLeft">
                <Space size={4} wrap>
                  {tagNodes}
                  <Typography.Text type="secondary">
                    {t("dataQuality.reports.detail.samplesTable.moreSamples", {
                      count: more,
                    })}
                  </Typography.Text>
                </Space>
              </Tooltip>
            );
          }
          return <Space size={4} wrap>{tagNodes}</Space>;
        },
      },
      {
        title: t("dataQuality.reports.detail.samplesTable.sampleSize"),
        dataIndex: "sampleSize",
        key: "sampleSize",
        width: 90,
        render: (v: number) => v.toLocaleString(),
      },
      {
        title: t("dataQuality.reports.detail.samplesTable.capturedAt"),
        dataIndex: "capturedAt",
        key: "capturedAt",
        width: 170,
        render: (v: string) => (v ? new Date(v).toLocaleString() : "—"),
      },
    ],
    [t],
  );

  if (!loading && samples.length === 0) {
    return (
      <Empty
        description={t("dataQuality.reports.detail.samplesTable.empty")}
      />
    );
  }

  return (
    <Table<ViolationSampleRead>
      rowKey="id"
      dataSource={samples}
      columns={columns}
      loading={loading ?? false}
      size="small"
      pagination={{ pageSize: 20, showSizeChanger: false }}
    />
  );
}