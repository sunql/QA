/** 数据质量评估报告 — 新建页（feat-dq-evaluation-report-wizard，2026-09-15）。
 *
 * 4 步向导（antd Steps）：
 *  Step 1 选对象（ontology class，多选）
 *  Step 2 为每个对象选规则（每对象一个 Card + Select）
 *  Step 3 组合列表 + 调整（可逐条删 / 整对象删）
 *  Step 4 时间窗口 + 元数据 + 提交
 *
 * 提交后跳转到 detail 页；detail 页会按进度轮询
 * `GET /api/v1/data-quality/reports/{id}/progress`（运行中状态显示 antd Progress）。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  App,
  Button,
  Card,
  DatePicker,
  Form,
  Input,
  Radio,
  Select,
  Space,
  Steps,
  Table,
  Tag,
  Typography,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs, { type Dayjs } from "dayjs";
import { useTranslation } from "../i18n";
import { createReport } from "../api/evaluationReport";
import { listClasses } from "../api/ontology";
import { listRules } from "../api/dataQuality";
import type {
  EvaluationReportCreate,
  ReportStatus,
} from "../types/evaluationReport";
import type { OntologyClass } from "../types/ontology";
import type { DataQualityRule } from "../types/dataQuality";

const { RangePicker } = DatePicker;
const { TextArea } = Input;

interface CombinedRow {
  /** 稳定 rowKey */
  key: string;
  classId: number;
  className: string;
  ruleId: number;
  ruleCode: string;
  ruleName: string;
  ruleType: string;
  targetTable: string;
}

interface CreateFormValues {
  name: string;
  description?: string;
  timeWindow: [Dayjs, Dayjs];
  tags?: string[];
  status: ReportStatus;
}

function defaultTimeWindow(): [Dayjs, Dayjs] {
  return [dayjs().subtract(7, "day").startOf("day"), dayjs().endOf("day")];
}

export default function DataQualityReportCreatePage() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const [form] = Form.useForm<CreateFormValues>();

  const [currentStep, setCurrentStep] = useState<number>(0);
  const [submitting, setSubmitting] = useState(false);
  const [classes, setClasses] = useState<OntologyClass[]>([]);
  const [allRules, setAllRules] = useState<DataQualityRule[]>([]);

  // Step 1：选中的对象（class id 列表）
  const [selectedClassIds, setSelectedClassIds] = useState<number[]>([]);
  // Step 2：classId → 该对象下用户勾选的规则 id 集合
  const [rulesByClass, setRulesByClass] = useState<Record<number, number[]>>({});

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cs = await listClasses();
        if (!cancelled) setClasses(cs);
      } catch {
        // ignore
      }
      try {
        const rs = await listRules({ enabled: "enabled" });
        if (!cancelled) setAllRules(rs);
      } catch {
        // ignore
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const classOptions = useMemo(
    () => classes.map((c) => ({ label: c.className, value: c.id })),
    [classes],
  );

  const ruleById = useMemo(() => {
    const m = new Map<number, DataQualityRule>();
    for (const r of allRules) m.set(r.id, r);
    return m;
  }, [allRules]);

  // Step 1：切换对象时，若新对象之前没有任何规则选择，默认空数组（用户可后续勾选）
  const handleClassChange = useCallback(
    (next: number[]) => {
      setSelectedClassIds(next);
      setRulesByClass((prev) => {
        const out: Record<number, number[]> = {};
        for (const cid of next) {
          out[cid] = prev[cid] ?? [];
        }
        return out;
      });
    },
    [],
  );

  // Step 2：单对象规则选择
  const setRulesForClass = useCallback((classId: number, ruleIds: number[]) => {
    setRulesByClass((prev) => ({ ...prev, [classId]: ruleIds }));
  }, []);

  // Step 3 组合列表：根据当前 selectedClassIds + rulesByClass 平展
  const combinedRows = useMemo<CombinedRow[]>(() => {
    const rows: CombinedRow[] = [];
    for (const cid of selectedClassIds) {
      const cls = classes.find((c) => c.id === cid);
      const className = cls?.className ?? `#${cid}`;
      for (const rid of rulesByClass[cid] ?? []) {
        const r = ruleById.get(rid);
        if (!r) continue;
        rows.push({
          key: `${cid}-${rid}`,
          classId: cid,
          className,
          ruleId: rid,
          ruleCode: r.ruleCode,
          ruleName: r.ruleName,
          ruleType: r.ruleType,
          targetTable: r.targetTable,
        });
      }
    }
    return rows;
  }, [selectedClassIds, rulesByClass, classes, ruleById]);

  // Step 3：删除单条组合
  const handleRemoveRow = useCallback((row: CombinedRow) => {
    setRulesByClass((prev) => ({
      ...prev,
      [row.classId]: (prev[row.classId] ?? []).filter((id) => id !== row.ruleId),
    }));
  }, []);

  // Step 3：删除整个对象（连带其规则）
  const handleRemoveClass = useCallback((classId: number) => {
    setSelectedClassIds((prev) => prev.filter((id) => id !== classId));
    setRulesByClass((prev) => {
      const { [classId]: _drop, ...rest } = prev;
      return rest;
    });
  }, []);

  const combinedColumns: ColumnsType<CombinedRow> = useMemo(
    () => [
      {
        title: "对象",
        dataIndex: "className",
        key: "className",
        width: 160,
        render: (v: string, row) => (
          <Space>
            <Tag color="blue">{v}</Tag>
            <Button
              type="link"
              size="small"
              danger
              onClick={() => handleRemoveClass(row.classId)}
            >
              移除对象
            </Button>
          </Space>
        ),
      },
      {
        title: "规则编码",
        dataIndex: "ruleCode",
        key: "ruleCode",
        width: 220,
      },
      {
        title: "规则名称",
        dataIndex: "ruleName",
        key: "ruleName",
        width: 220,
      },
      {
        title: "规则类型",
        dataIndex: "ruleType",
        key: "ruleType",
        width: 140,
        render: (rt: string) => t(`dataQuality.ruleTypeLabels.${rt}`),
      },
      {
        title: "目标表",
        dataIndex: "targetTable",
        key: "targetTable",
        width: 160,
      },
      {
        title: "操作",
        key: "action",
        width: 100,
        render: (_, row) => (
          <Button type="link" size="small" danger onClick={() => handleRemoveRow(row)}>
            移除
          </Button>
        ),
      },
    ],
    [handleRemoveRow, handleRemoveClass, t],
  );

  const handleSubmit = useCallback(async () => {
    // Step 4 提交：校验表单 + 组装 payload
    const values = await form.validateFields();
    if (combinedRows.length === 0) {
      void message.error("请至少选择 1 条规则");
      return;
    }
    setSubmitting(true);
    try {
      const classIds = Array.from(new Set(combinedRows.map((r) => r.classId)));
      const ruleIds = Array.from(new Set(combinedRows.map((r) => r.ruleId)));
      const payload: EvaluationReportCreate = {
        name: values.name,
        description: values.description ?? null,
        classIds,
        ruleIds,
        timeWindowStart: values.timeWindow[0].toISOString(),
        timeWindowEnd: values.timeWindow[1].toISOString(),
        tags: values.tags ?? [],
        // 后端 createReport 内部会把非 DRAFT/PUBLISHED 强制转 PENDING
        status: values.status === "DRAFT" ? "DRAFT" : "PUBLISHED",
      };
      const created = await createReport(payload);
      void message.success(t("dataQuality.reports.create.success"));
      navigate(`/data-quality/reports/${created.id}`);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      void message.error(msg);
    } finally {
      setSubmitting(false);
    }
  }, [form, combinedRows, message, navigate, t]);

  // Step 2 子卡片：每个对象一张卡 + Select
  const stepTwoCards = useMemo(() => {
    if (selectedClassIds.length === 0) {
      return (
        <Typography.Text type="secondary">请先在第 1 步选择对象。</Typography.Text>
      );
    }
    return selectedClassIds.map((cid) => {
      const cls = classes.find((c) => c.id === cid);
      const className = cls?.className ?? `#${cid}`;
      const value = rulesByClass[cid] ?? [];
      // 仅显示该对象下的规则（r.sourceClassId === cid）。
      // sourceClassId 为 null 的老规则不进选择面，与用户「只看到对象下规则」一致。
      const opts = allRules
        .filter((r) => r.sourceClassId === cid)
        .map((r) => ({
          label: `${r.ruleCode}（${r.targetTable} / ${r.ruleType}）`,
          value: r.id,
        }));
      return (
        <Card
          key={cid}
          size="small"
          title={className}
          extra={
            <Button type="link" size="small" danger onClick={() => handleRemoveClass(cid)}>
              移除该对象
            </Button>
          }
          style={{ marginBottom: 12 }}
        >
          <Select
            mode="multiple"
            showSearch
            optionFilterProp="label"
            style={{ width: "100%" }}
            placeholder={`为「${className}」选择要评估的规则`}
            value={value}
            onChange={(v) => setRulesForClass(cid, v as number[])}
            options={opts}
          />
        </Card>
      );
    });
  }, [
    selectedClassIds,
    classes,
    rulesByClass,
    allRules,
    setRulesForClass,
    handleRemoveClass,
  ]);

  // 上 / 下一步禁用判断
  const canGoNextFrom1 = selectedClassIds.length > 0;
  const canGoNextFrom2 = combinedRows.length > 0;

  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ marginTop: 0 }}>{t("dataQuality.reports.create.title")}</h2>

      <Card style={{ marginBottom: 16 }}>
        <Steps
          current={currentStep}
          items={[
            { title: "选择对象" },
            { title: "选择规则" },
            { title: "确认组合" },
            { title: "时间窗口与提交" },
          ]}
        />
      </Card>

      <Card>
        {currentStep === 0 && (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Typography.Text>
              选择本次评估的对象（本体类），可多选。后续为每个对象单独挑选规则。
            </Typography.Text>
            <Select
              mode="multiple"
              style={{ width: "100%" }}
              placeholder="选择对象"
              value={selectedClassIds}
              onChange={handleClassChange}
              options={classOptions}
              showSearch
              optionFilterProp="label"
            />
          </Space>
        )}

        {currentStep === 1 && <div>{stepTwoCards}</div>}

        {currentStep === 2 && (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Typography.Text>
              当前组合（{combinedRows.length} 条规则）。可逐条移除或移除整个对象后回到上一步补充。
            </Typography.Text>
            {combinedRows.length === 0 ? (
              <Typography.Text type="secondary">
                组合为空，请回到第 2 步至少选择 1 条规则。
              </Typography.Text>
            ) : (
              <Table<CombinedRow>
                rowKey="key"
                dataSource={combinedRows}
                columns={combinedColumns}
                size="small"
                pagination={{ pageSize: 10, showSizeChanger: false }}
              />
            )}
          </Space>
        )}

        {currentStep === 3 && (
          <Form<CreateFormValues>
            form={form}
            layout="vertical"
            initialValues={{
              timeWindow: defaultTimeWindow(),
              status: "PUBLISHED",
              tags: [],
            }}
          >
            <Form.Item
              label={t("dataQuality.reports.create.name")}
              name="name"
              rules={[
                {
                  required: true,
                  message: t("dataQuality.reports.create.nameRequired"),
                },
              ]}
            >
              <Input maxLength={200} showCount />
            </Form.Item>

            <Form.Item
              label={t("dataQuality.reports.create.description")}
              name="description"
            >
              <TextArea rows={3} maxLength={1000} />
            </Form.Item>

            <Form.Item
              label={t("dataQuality.reports.create.timeWindow")}
              name="timeWindow"
              rules={[{ required: true }]}
            >
              <RangePicker
                showTime={{ format: "HH:mm" }}
                format="YYYY-MM-DD HH:mm"
                style={{ width: "100%" }}
              />
            </Form.Item>

            <Form.Item label={t("dataQuality.reports.create.tags")} name="tags">
              <Select mode="tags" tokenSeparators={[","]} />
            </Form.Item>

            <Form.Item
              label={t("dataQuality.reports.create.status")}
              name="status"
              extra="选 DRAFT 可继续编辑；选 PUBLISHED 提交后对所有登录用户可见。"
            >
              <Radio.Group>
                <Radio.Button value="DRAFT">
                  {t("dataQuality.reports.statusLabels.DRAFT")}
                </Radio.Button>
                <Radio.Button value="PUBLISHED">
                  {t("dataQuality.reports.statusLabels.PUBLISHED")}
                </Radio.Button>
              </Radio.Group>
            </Form.Item>
          </Form>
        )}
      </Card>

      <Space style={{ marginTop: 16 }}>
        <Button
          disabled={currentStep === 0}
          onClick={() => setCurrentStep((s) => Math.max(0, s - 1))}
        >
          上一步
        </Button>
        {currentStep < 3 && (
          <Button
            type="primary"
            disabled={
              (currentStep === 0 && !canGoNextFrom1) ||
              (currentStep === 1 && !canGoNextFrom2) ||
              (currentStep === 2 && combinedRows.length === 0)
            }
            onClick={() => setCurrentStep((s) => Math.min(3, s + 1))}
          >
            下一步
          </Button>
        )}
        {currentStep === 3 && (
          <Button
            type="primary"
            loading={submitting}
            onClick={() => void handleSubmit()}
          >
            {t("dataQuality.reports.create.submit")}
          </Button>
        )}
        <Button onClick={() => navigate(-1)}>
          {t("common.cancel", { defaultValue: "取消" })}
        </Button>
      </Space>
    </div>
  );
}
