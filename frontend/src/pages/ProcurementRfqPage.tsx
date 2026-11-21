/** M4-1 询价草稿页（M5 API 消费）：列表 + 创建 + 详情（决策链）+ 操作。
 *
 * 口径对齐后端（M5，DRAFT-only）：
 * - 状态机由后端裁决（前端只按 status 决定可用动作）；
 * - 自批禁止在服务端（创建人看不到自己待审单的审批按钮）；
 * - **无发送操作**（D-4：SENDING/SENT 在 M5 不可达）。
 */

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  DatePicker,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { useTranslation } from "../i18n";
import { useAuthStore } from "../stores/authStore";
import {
  availableRfqActions,
  resolveProcurementPermissions,
  selfApprovalHintKey,
} from "../utils/procurementPermissions";
import {
  cancelRfqDraft,
  createRfqDraft,
  decideRfqApproval,
  getRfqDraft,
  listRfqDrafts,
  submitRfqApproval,
  validateRfqDraft,
  type RfqDraft,
  type RfqDecision,
  type RfqStatus,
} from "../api/procurementRfq";

const STATUS_COLOR: Record<RfqStatus, string> = {
  DRAFT: "default",
  VALIDATING: "processing",
  NEEDS_CORRECTION: "warning",
  PENDING_APPROVAL: "processing",
  READY_TO_SEND: "success",
  REJECTED: "error",
  CANCELLED: "default",
  SENDING: "processing",
  SENT: "success",
  CLOSED: "default",
};

const DECISION_COLOR: Record<string, string> = {
  SUBMIT: "processing",
  APPROVE: "success",
  REJECT: "error",
  CANCEL: "default",
};

interface CreateFormValues {
  requestId: string;
  deadline: Date | null;
  supplierKeys: string;
  materialKey: number | null;
  quantity: number | null;
  unit: string | null;
  deliveryDate: string | null;
  notes: string | null;
}

export default function ProcurementRfqPage() {
  const { t } = useTranslation();
  const user = useAuthStore((s) => s.user);
  const [drafts, setDrafts] = useState<RfqDraft[]>([]);
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<RfqDraft | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [form] = Form.useForm<CreateFormValues>();

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setDrafts(await listRfqDrafts());
    } catch {
      message.error(t("procurement.rfq.loadFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const openDetail = useCallback(
    async (rfqDraftId: string) => {
      setDetailLoading(true);
      try {
        setDetail(await getRfqDraft(rfqDraftId));
      } catch {
        message.error(t("procurement.rfq.loadFailed"));
      } finally {
        setDetailLoading(false);
      }
    },
    [t],
  );

  const act = useCallback(
    async (fn: () => Promise<RfqDraft>, successText: string) => {
      try {
        const updated = await fn();
        message.success(successText);
        setDetail(updated);
        await reload();
      } catch (err) {
        message.error(
          err instanceof Error ? err.message : t("procurement.rfq.actionFailed"),
        );
      }
    },
    [reload, t],
  );

  const submitCreate = useCallback(
    async (values: CreateFormValues) => {
      try {
        await createRfqDraft({
          requestId: values.requestId,
          deadline: values.deadline
            ? dayjs(values.deadline).toISOString()
            : new Date().toISOString(),
          supplierKeys: (values.supplierKeys ?? "")
            .split(/[,\s]+/)
            .filter(Boolean)
            .map(Number)
            .filter((n) => !Number.isNaN(n)),
          lineItems:
            values.materialKey != null && values.quantity != null
              ? [
                  {
                    materialKey: values.materialKey,
                    quantity: values.quantity,
                    unit: values.unit ?? "PCS",
                    deliveryDate:
                      values.deliveryDate ?? dayjs().format("YYYY-MM-DD"),
                  },
                ]
              : [],
          notes: values.notes ?? null,
        });
        message.success(t("procurement.rfq.createSuccess"));
        setCreateOpen(false);
        form.resetFields();
        await reload();
      } catch (err) {
        message.error(
          err instanceof Error ? err.message : t("procurement.rfq.actionFailed"),
        );
      }
    },
    [form, reload, t],
  );

  const columns: ColumnsType<RfqDraft> = [
    {
      title: t("procurement.rfq.col.requestId"),
      dataIndex: "requestId",
      key: "requestId",
    },
    {
      title: t("procurement.rfq.col.status"),
      dataIndex: "status",
      key: "status",
      render: (status: RfqStatus) => (
        <Tag color={STATUS_COLOR[status] ?? "default"}>{status}</Tag>
      ),
    },
    {
      title: t("procurement.rfq.col.deadline"),
      dataIndex: "deadline",
      key: "deadline",
      render: (v: string) => (v ? v.slice(0, 10) : "-"),
    },
    {
      title: t("procurement.rfq.col.createdBy"),
      dataIndex: "createdBy",
      key: "createdBy",
    },
    {
      title: "",
      key: "actions",
      render: (_, record) => (
        <Button size="small" onClick={() => void openDetail(record.rfqDraftId)}>
          {t("procurement.rfq.openDetail")}
        </Button>
      ),
    },
  ];

  const decisionColumns: ColumnsType<RfqDecision> = [
    {
      title: t("procurement.rfq.decision.decision"),
      dataIndex: "decision",
      key: "decision",
      render: (d: string) => (
        <Tag color={DECISION_COLOR[d] ?? "default"}>{d}</Tag>
      ),
    },
    { title: t("procurement.rfq.decision.actor"), dataIndex: "actor", key: "actor" },
    {
      title: t("procurement.rfq.decision.time"),
      dataIndex: "decidedTime",
      key: "decidedTime",
      render: (v: string | null) => (v ? v.slice(0, 19).replace("T", " ") : "-"),
    },
    {
      title: t("procurement.rfq.decision.note"),
      dataIndex: "note",
      key: "note",
      render: (v: string | null) => v ?? "-",
    },
  ];

  const statusActions = detail
    ? buildActions(
        detail,
        t,
        act,
        resolveProcurementPermissions(user?.roles),
        detail.createdBy === user?.username,
      )
    : [];

  return (
    <div style={{ padding: 24 }}>
      <Space direction="vertical" style={{ width: "100%" }} size="large">
        <Space style={{ width: "100%", justifyContent: "space-between" }}>
          <h2>{t("procurement.rfq.title")}</h2>
          <Button type="primary" onClick={() => setCreateOpen(true)}>
            {t("procurement.rfq.create")}
          </Button>
        </Space>
        <Table
          rowKey="rfqDraftId"
          columns={columns}
          dataSource={drafts}
          loading={loading}
          pagination={{ pageSize: 10, showSizeChanger: false }}
        />
      </Space>

      <Modal
        open={createOpen}
        title={t("procurement.rfq.create")}
        onCancel={() => setCreateOpen(false)}
        onOk={() => void form.submit()}
        okText={t("procurement.rfq.submitCreate")}
        destroyOnClose
      >
        <Form form={form} layout="vertical" onFinish={submitCreate}>
          <Form.Item
            name="requestId"
            label={t("procurement.rfq.form.requestId")}
            rules={[{ required: true }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="deadline"
            label={t("procurement.rfq.form.deadline")}
            rules={[{ required: true }]}
          >
            <DatePicker showTime style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item
            name="supplierKeys"
            label={t("procurement.rfq.form.supplierKeys")}
            rules={[{ required: true }]}
          >
            <Input placeholder="900001, 900002" />
          </Form.Item>
          <Space>
            <Form.Item name="materialKey" label={t("procurement.rfq.form.materialKey")}>
              <InputNumber />
            </Form.Item>
            <Form.Item name="quantity" label={t("procurement.rfq.form.quantity")}>
              <InputNumber min={1} />
            </Form.Item>
            <Form.Item name="unit" label={t("procurement.rfq.form.unit")}>
              <Input placeholder="PCS" />
            </Form.Item>
          </Space>
          <Form.Item name="deliveryDate" label={t("procurement.rfq.form.deliveryDate")}>
            <Input placeholder="2026-12-05" />
          </Form.Item>
          <Form.Item name="notes" label={t("procurement.rfq.form.notes")}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        open={detail != null}
        title={detail?.requestId ?? ""}
        width={640}
        onClose={() => setDetail(null)}
        destroyOnClose
      >
        {detail != null && (
          <Space direction="vertical" style={{ width: "100%" }} size="middle">
            <Descriptions size="small" column={1} bordered>
              <Descriptions.Item label={t("procurement.rfq.detail.status")}>
                <Tag color={STATUS_COLOR[detail.status] ?? "default"}>
                  {detail.status}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t("procurement.rfq.detail.version")}>
                v{detail.version}
              </Descriptions.Item>
              <Descriptions.Item label={t("procurement.rfq.detail.suppliers")}>
                {detail.supplierKeys.join(", ") || "-"}
              </Descriptions.Item>
              <Descriptions.Item label={t("procurement.rfq.detail.deadline")}>
                {detail.deadline.slice(0, 19).replace("T", " ")}
              </Descriptions.Item>
              <Descriptions.Item label={t("procurement.rfq.detail.notes")}>
                {detail.notes ?? "-"}
              </Descriptions.Item>
            </Descriptions>

            {statusActions.length > 0 && (
              <Space>{statusActions}</Space>
            )}

            <h4>{t("procurement.rfq.decisionsTitle")}</h4>
            <Table
              rowKey={(r, i) => `${r.decision}-${i}`}
              columns={decisionColumns}
              dataSource={detail.decisions}
              pagination={false}
              size="small"
              loading={detailLoading}
            />
          </Space>
        )}
      </Drawer>
    </div>
  );
}

interface ActFn {
  (fn: () => Promise<RfqDraft>, successText: string): Promise<void>;
}

type TFn = (key: string) => string;

/** 按 status + 权限矩阵生成可用动作（后端状态机与角色闸是裁决者，前端只做入口裁剪）。 */
function buildActions(
  draft: RfqDraft,
  t: TFn,
  act: ActFn,
  permissions: ReturnType<typeof resolveProcurementPermissions>,
  isCreator: boolean,
): React.ReactNode[] {
  const actions: React.ReactNode[] = [];
  const v = draft.version;
  const id = draft.rfqDraftId;
  const allowed = new Set(
    availableRfqActions(draft.status, permissions, isCreator),
  );
  // 自批提示（服务端会拒自批 ⇒ 前端先说清）
  const hintKey = selfApprovalHintKey(isCreator);
  if (hintKey != null && draft.status === "PENDING_APPROVAL") {
    actions.push(
      <Alert key="self-approval-hint" type="info" showIcon message={t(hintKey)} />,
    );
  }
  if (draft.status === "DRAFT" && allowed.has("validate")) {
    actions.push(
      <Popconfirm
        key="validate"
        title={t("procurement.rfq.confirm.validate")}
        onConfirm={() => void act(() => validateRfqDraft(id, v), t("procurement.rfq.done.validate"))}
      >
        <Button type="primary" size="small">
          {t("procurement.rfq.action.validate")}
        </Button>
      </Popconfirm>,
    );
    if (allowed.has("cancel")) {
      actions.push(
        <Popconfirm key="cancel" title={t("procurement.rfq.confirm.cancel")}
        onConfirm={() => void act(() => cancelRfqDraft(id, v), t("procurement.rfq.done.cancel"))}
        >
          <Button danger size="small">{t("procurement.rfq.action.cancel")}</Button>
        </Popconfirm>,
      );
    }
  }
  if (draft.status === "VALIDATING" && allowed.has("submit")) {
    actions.push(
      <Popconfirm key="submit" title={t("procurement.rfq.confirm.submit")}
        onConfirm={() => void act(() => submitRfqApproval(id), t("procurement.rfq.done.submit"))}
      >
        <Button type="primary" size="small">
          {t("procurement.rfq.action.submit")}
        </Button>
      </Popconfirm>,
    );
  }
  // PENDING_APPROVAL 的 approve/reject 在**审批人视角**渲染（自批禁止由服务端裁决）。
  if (draft.status === "PENDING_APPROVAL" && allowed.has("approve")) {
    actions.push(
      <Popconfirm key="approve" title={t("procurement.rfq.confirm.approve")}
        onConfirm={() => void act(() => decideRfqApproval(id, "APPROVE"), t("procurement.rfq.done.approve"))}
      >
        <Button type="primary" size="small">
          {t("procurement.rfq.action.approve")}
        </Button>
      </Popconfirm>,
    );
    if (allowed.has("reject")) {
      actions.push(
        <Popconfirm key="reject" title={t("procurement.rfq.confirm.reject")}
        onConfirm={() => void act(() => decideRfqApproval(id, "REJECT"), t("procurement.rfq.done.reject"))}
        >
          <Button danger size="small">{t("procurement.rfq.action.reject")}</Button>
        </Popconfirm>,
      );
    }
  }
  return actions;
}
