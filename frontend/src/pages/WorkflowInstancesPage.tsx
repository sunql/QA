/** P5 工作流实例页：实例列表 + 详情（步骤时间线 / 审批记录）+ 提交表单。
 *
 * 三个诉求（最小前端批次）：
 * 1. **列表**：服务端分页，支持按状态过滤；
 * 2. **详情**：抽屉里看步骤时间线与审批记录（不跳页，保留列表上下文）；
 * 3. **提交**：简单表单（选已发布定义 + 幂等键 + 请求 JSON）—— 不依赖
 *    业务对象页跳转，任何登录用户都能从这里发起。
 *
 * 口径：后端只读接口已经做了授权（自作用域 / 审批持有人 / admin），前端
 * 不做二次过滤 —— 列表里看得到的，详情就一定能打开。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  Descriptions,
  Drawer,
  Form,
  Input,
  message,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Timeline,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";
import { useTablePagination } from "../utils/useTablePagination";
import {
  cancelWorkflowInstance,
  getWorkflowInstance,
  listWorkflowDefinitions,
  listWorkflowInstances,
  submitWorkflowInstance,
} from "../api/workflow";
import type {
  WorkflowDefinitionSummary,
  WorkflowInstanceDetail,
  WorkflowInstanceListItem,
} from "../types/workflow";

/** 实例终态：取消按钮只在「还会再写外部系统」的实例上出现。 */
const STOPPED_STATUSES = new Set([
  "COMPLETED",
  "FAILED",
  "CANCELLED",
  "REJECTED",
  "COMPENSATED",
]);

const INSTANCE_STATUS_OPTIONS = [
  "PENDING",
  "RUNNING",
  "WAITING_APPROVAL",
  "APPROVED",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
];

const STATUS_COLORS: Record<string, string> = {
  PENDING: "default",
  RUNNING: "processing",
  WAITING_APPROVAL: "gold",
  APPROVED: "blue",
  REJECTED: "red",
  COMPLETED: "green",
  FAILED: "red",
  CANCELLED: "default",
  COMPENSATED: "purple",
  SUCCEEDED: "green",
  SKIPPED: "default",
  TIMEOUT: "gray",
};

const STEP_COLORS: Record<string, string> = {
  PENDING: "gray",
  RUNNING: "blue",
  SUCCEEDED: "green",
  FAILED: "red",
  SKIPPED: "gray",
  COMPENSATED: "purple",
};

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `wf-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function statusTag(status: string, t: (key: string) => string) {
  return <Tag color={STATUS_COLORS[status]}>{t(`workflow.status.${status}`)}</Tag>;
}

export default function WorkflowInstancesPage() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<WorkflowInstanceListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState<string | undefined>();
  const [detail, setDetail] = useState<WorkflowInstanceDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [submitOpen, setSubmitOpen] = useState(false);
  const [definitions, setDefinitions] = useState<WorkflowDefinitionSummary[]>([]);
  // 表单初始值放进 ref，等 Modal 完全打开后在 afterOpenChange 写 ——
  // destroyOnHidden + preserve=false 的重挂载时序会吞掉提前写入的值
  // （jsdom 不复现这条路径，测试通过 ≠ 真机可用）。
  const submitDraftRef = useRef<{ idempotencyKey: string; requestJson: string } | null>(
    null,
  );
  const { pagination, state, setPage } = useTablePagination({
    serverTotal: total,
  });
  const [form] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const page = await listWorkflowInstances({
        status: statusFilter,
        limit: state.pageSize,
        offset: (state.current - 1) * state.pageSize,
      });
      setRows(page.rows);
      setTotal(page.total);
    } catch (error: unknown) {
      const detailText = error instanceof Error ? error.message : String(error);
      void message.error(t("workflow.messages.loadFailed", { detail: detailText }));
    } finally {
      setLoading(false);
    }
  }, [statusFilter, state.current, state.pageSize, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const onOpenDetail = async (instanceId: number) => {
    try {
      setDetail(await getWorkflowInstance(instanceId));
      setDetailOpen(true);
    } catch (error: unknown) {
      const detailText = error instanceof Error ? error.message : String(error);
      void message.error(t("workflow.messages.loadFailed", { detail: detailText }));
    }
  };

  const onOpenSubmit = async () => {
    try {
      setDefinitions(await listWorkflowDefinitions());
    } catch (error: unknown) {
      const detailText = error instanceof Error ? error.message : String(error);
      void message.error(t("workflow.messages.loadFailed", { detail: detailText }));
      return;
    }
    submitDraftRef.current = {
      idempotencyKey: newIdempotencyKey(),
      requestJson: "{}",
    };
    setSubmitOpen(true);
  };

  const onSubmit = async () => {
    let values;
    try {
      values = await form.validateFields();
    } catch {
      // antd validateFields 校验失败时 reject；错误提示已由表单展示
      return;
    }
    const definition = definitions.find((d) => d.id === values.definitionId);
    if (!definition) {
      void message.error(t("workflow.messages.submitFailed", { detail: "定义不存在" }));
      return;
    }
    try {
      await submitWorkflowInstance({
        domainCode: definition.domainCode,
        capabilityCode: definition.capabilityCode,
        version: definition.version,
        idempotencyKey: values.idempotencyKey,
        request: JSON.parse(values.requestJson),
      });
      setSubmitOpen(false);
      setPage(1);
      await load();
      void message.success(t("workflow.messages.submitOk"));
    } catch (error: unknown) {
      const detailText = error instanceof Error ? error.message : String(error);
      void message.error(t("workflow.messages.submitFailed", { detail: detailText }));
    }
  };

  const onCancel = async (instanceId: number) => {
    try {
      await cancelWorkflowInstance(instanceId, t("workflow.cancelReason"));
      await load();
      void message.success(t("workflow.messages.cancelOk"));
    } catch (error: unknown) {
      const detailText = error instanceof Error ? error.message : String(error);
      void message.error(t("workflow.messages.cancelFailed", { detail: detailText }));
    }
  };

  const definitionOptions = useMemo(
    () =>
      definitions.map((d) => ({
        value: d.id,
        label: `${d.domainCode} / ${d.capabilityCode} @ ${d.version}`,
      })),
    [definitions],
  );

  const columns: ColumnsType<WorkflowInstanceListItem> = [
    { title: t("workflow.columns.id"), dataIndex: "id", width: 70 },
    {
      title: t("workflow.columns.capability"),
      key: "capability",
      render: (_, row) => `${row.domainCode} / ${row.capabilityCode}`,
    },
    { title: t("workflow.columns.version"), dataIndex: "definitionVersion", width: 90 },
    {
      title: t("workflow.columns.status"),
      dataIndex: "status",
      width: 130,
      render: (status: string) => statusTag(status, t),
    },
    { title: t("workflow.columns.createdBy"), dataIndex: "createdBy", width: 120 },
    { title: t("workflow.columns.createdTime"), dataIndex: "createdTime", width: 180 },
    {
      title: t("workflow.columns.actions"),
      key: "actions",
      width: 180,
      render: (_, row) => (
        <Space>
          <Button size="small" onClick={() => void onOpenDetail(row.id)}>
            {t("workflow.actions.view")}
          </Button>
          {!STOPPED_STATUSES.has(row.status) && (
            <Popconfirm
              title={t("workflow.actions.cancelConfirm", { id: row.id })}
              onConfirm={() => void onCancel(row.id)}
            >
              <Button size="small" danger>
                {t("workflow.actions.cancel")}
              </Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ marginBottom: 16 }}>{t("workflow.title")}</h2>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" onClick={() => void onOpenSubmit()}>
          {t("workflow.submitButton")}
        </Button>
        <Select
          allowClear
          style={{ width: 200 }}
          placeholder={t("workflow.filters.status")}
          value={statusFilter}
          onChange={(value) => {
            setStatusFilter(value);
            setPage(1);
          }}
          options={INSTANCE_STATUS_OPTIONS.map((status) => ({
            value: status,
            label: t(`workflow.status.${status}`),
          }))}
        />
        <Button onClick={() => void load()}>{t("workflow.refresh")}</Button>
      </Space>
      <Table
        loading={loading}
        dataSource={rows}
        pagination={pagination}
        rowKey="id"
        columns={columns}
      />
      <Drawer
        open={detailOpen}
        width={720}
        title={detail ? `${t("workflow.detail.title")} #${detail.instance.id}` : ""}
        onClose={() => setDetailOpen(false)}
      >
        {detail && <InstanceDetail detail={detail} />}
      </Drawer>
      <Modal
        open={submitOpen}
        title={t("workflow.submit.title")}
        onCancel={() => setSubmitOpen(false)}
        onOk={() => void onSubmit()}
        destroyOnHidden
        afterOpenChange={(open) => {
          // 弹窗完全打开后再写值 —— 此时 Form 子组件已挂载。
          if (open && submitDraftRef.current) {
            form.setFieldsValue(submitDraftRef.current);
          }
        }}
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item
            name="definitionId"
            label={t("workflow.submit.definition")}
            rules={[{ required: true }]}
          >
            <Select
              placeholder={t("workflow.submit.definitionPlaceholder")}
              options={definitionOptions}
            />
          </Form.Item>
          <Form.Item
            name="idempotencyKey"
            label={t("workflow.submit.idempotencyKey")}
            rules={[{ required: true }, { max: 128 }]}
          >
            <Input maxLength={128} />
          </Form.Item>
          <Form.Item
            name="requestJson"
            label={t("workflow.submit.requestJson")}
            rules={[
              { required: true },
              {
                validator: async (_, value: string) => {
                  try {
                    JSON.parse(value);
                  } catch {
                    throw new Error(t("workflow.submit.requestJsonInvalid"));
                  }
                },
              },
            ]}
          >
            <Input.TextArea rows={6} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function InstanceDetail({ detail }: { detail: WorkflowInstanceDetail }) {
  const { t } = useTranslation();
  const { instance, steps, approvals } = detail;
  return (
    <Space direction="vertical" style={{ width: "100%" }} size="large">
      <Descriptions column={2} size="small" bordered>
        <Descriptions.Item label={t("workflow.columns.status")}>
          {statusTag(instance.status, t)}
        </Descriptions.Item>
        <Descriptions.Item label={t("workflow.columns.createdBy")}>
          {instance.createdBy ?? "—"}
        </Descriptions.Item>
        <Descriptions.Item label={t("workflow.detail.idempotencyKey")}>
          {instance.idempotencyKey}
        </Descriptions.Item>
        <Descriptions.Item label={t("workflow.columns.createdTime")}>
          {instance.createdTime}
        </Descriptions.Item>
        {instance.errorMessage && (
          <Descriptions.Item label={t("workflow.detail.errorMessage")} span={2}>
            {instance.errorMessage}
          </Descriptions.Item>
        )}
      </Descriptions>
      <div>
        <h4>{t("workflow.detail.steps")}</h4>
        {steps.length === 0 ? (
          <span>{t("workflow.detail.noSteps")}</span>
        ) : (
          <Timeline
            items={steps.map((step) => ({
              color: STEP_COLORS[step.status] ?? "gray",
              children: (
                <div>
                  <strong>{step.stepCode}</strong>{" "}
                  <Tag color={STATUS_COLORS[step.status]}>{step.status}</Tag>
                  {step.errorMessage && <div>{step.errorMessage}</div>}
                </div>
              ),
            }))}
          />
        )}
      </div>
      <div>
        <h4>{t("workflow.detail.approvals")}</h4>
        <Table
          size="small"
          rowKey="id"
          pagination={false}
          dataSource={approvals}
          columns={[
            { title: t("workflow.columns.matrix"), dataIndex: "matrixCode" },
            {
              title: t("workflow.columns.status"),
              dataIndex: "status",
              render: (status: string) => statusTag(status, t),
            },
            {
              title: t("workflow.columns.approver"),
              dataIndex: "approverUser",
              render: (value: string | null) => value ?? "—",
            },
            { title: t("workflow.columns.dueAt"), dataIndex: "dueAt" },
            { title: t("workflow.columns.decisionNote"), dataIndex: "decisionNote" },
          ]}
        />
      </div>
    </Space>
  );
}
