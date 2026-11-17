/** P5 我的待办审批页：审批持有人的待办列表 + 通过/驳回。
 *
 * 口径（与后端 ``GET /workflow/approvals`` 一致）：
 * - 只回**当前身份**能批的任务；持有人匹配有三条来源（任务行 approver_role /
 *   审批矩阵 approverUser / approverRole），前端不做任何二次过滤；
 * - 后端要求**已校验身份**（stub 头可冒充任意账号）⇒ 403
 *   ``APPROVAL_REAL_AUTH_REQUIRED`` 时给出可执行的提示，而不是通用错误；
 * - 决策是单次的：成功后必须重新拉取，否则用户会对同一任务连点。
 */

import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Descriptions,
  Form,
  Input,
  message,
  Modal,
  Space,
  Table,
  Tag,
  Tooltip,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";
import { useTablePagination } from "../utils/useTablePagination";
import { decideWorkflowApproval, listMyApprovals } from "../api/workflow";
import type { AppError } from "../api/client";
import type { WorkflowApprovalListItem, WorkflowDecision } from "../types/workflow";

const REAL_AUTH_REQUIRED_CODE = "APPROVAL_REAL_AUTH_REQUIRED";

const STATUS_COLORS: Record<string, string> = {
  PENDING: "gold",
  APPROVED: "green",
  REJECTED: "red",
  TIMEOUT: "gray",
  CANCELLED: "default",
};

function isRealAuthRequired(error: unknown): boolean {
  const appError = error as Partial<AppError> | undefined;
  return (
    appError?.status === 403 &&
    typeof appError.detail === "string" &&
    appError.detail === REAL_AUTH_REQUIRED_CODE
  );
}

export default function WorkflowApprovalsPage() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<WorkflowApprovalListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [authBlocked, setAuthBlocked] = useState(false);
  const [pending, setPending] = useState<{
    task: WorkflowApprovalListItem;
    decision: WorkflowDecision;
  } | null>(null);
  const { pagination, state } = useTablePagination({ serverTotal: total });
  const [form] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const page = await listMyApprovals({
        status: "PENDING",
        limit: state.pageSize,
        offset: (state.current - 1) * state.pageSize,
      });
      setRows(page.rows);
      setTotal(page.total);
      setAuthBlocked(false);
    } catch (error: unknown) {
      if (isRealAuthRequired(error)) {
        setRows([]);
        setTotal(0);
        setAuthBlocked(true);
        return;
      }
      const detail = error instanceof Error ? error.message : String(error);
      void message.error(t("workflow.messages.loadFailed", { detail }));
    } finally {
      setLoading(false);
    }
  }, [state.current, state.pageSize, t]);

  useEffect(() => {
    void load();
  }, [load]);

  // 不在这里 resetFields：Modal 用 destroyOnHidden + preserve={false}，
  // 每次打开都是全新表单，提前 reset 反而会在未挂载时触发 antd 告警。
  const openDecision = (task: WorkflowApprovalListItem, decision: WorkflowDecision) => {
    setPending({ task, decision });
  };

  const onDecision = async () => {
    if (!pending) {
      return;
    }
    let values;
    try {
      values = await form.validateFields();
    } catch {
      // antd validateFields 校验失败时 reject；错误提示已由表单展示
      return;
    }
    try {
      await decideWorkflowApproval(pending.task.id, {
        decision: pending.decision,
        note: values.note ?? null,
      });
      setPending(null);
      await load();
      void message.success(t("workflow.messages.decisionOk"));
    } catch (error: unknown) {
      if (isRealAuthRequired(error)) {
        setPending(null);
        setAuthBlocked(true);
        return;
      }
      const detail = error instanceof Error ? error.message : String(error);
      void message.error(t("workflow.messages.decisionFailed", { detail }));
    }
  };

  const columns: ColumnsType<WorkflowApprovalListItem> = [
    { title: t("workflow.columns.id"), dataIndex: "id", width: 70 },
    {
      title: t("workflow.columns.instance"),
      dataIndex: "instanceId",
      width: 90,
    },
    {
      title: t("workflow.columns.capability"),
      key: "capability",
      render: (_, row) => `${row.domainCode} / ${row.capabilityCode}`,
    },
    { title: t("workflow.columns.matrix"), dataIndex: "matrixCode", width: 160 },
    {
      title: t("workflow.columns.subject"),
      key: "subject",
      render: (_, row) => (
        <Tooltip title={row.subjectId}>
          {row.subjectType}/#{row.subjectId}
        </Tooltip>
      ),
    },
    {
      title: t("workflow.columns.instanceStatus"),
      dataIndex: "instanceStatus",
      width: 140,
      render: (status: string) => (
        <Tag color={STATUS_COLORS[status]}>{t(`workflow.status.${status}`)}</Tag>
      ),
    },
    { title: t("workflow.columns.createdBy"), dataIndex: "instanceCreatedBy", width: 120 },
    { title: t("workflow.columns.dueAt"), dataIndex: "dueAt", width: 180 },
    {
      title: t("workflow.columns.actions"),
      key: "actions",
      width: 160,
      render: (_, row) => (
        <Space>
          <Button size="small" type="primary" onClick={() => openDecision(row, "APPROVE")}>
            {t("workflow.actions.approve")}
          </Button>
          <Button size="small" danger onClick={() => openDecision(row, "REJECT")}>
            {t("workflow.actions.reject")}
          </Button>
        </Space>
      ),
    },
  ];

  if (authBlocked) {
    return (
      <div style={{ padding: 24 }}>
        <h2 style={{ marginBottom: 16 }}>{t("workflow.approvalsTitle")}</h2>
        <Descriptions column={1} bordered size="small">
          <Descriptions.Item label={t("workflow.approvals.authRequiredLabel")}>
            {t("workflow.messages.realAuthRequired")}
          </Descriptions.Item>
        </Descriptions>
      </div>
    );
  }

  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ marginBottom: 16 }}>{t("workflow.approvalsTitle")}</h2>
      <Space style={{ marginBottom: 16 }}>
        <Button onClick={() => void load()}>{t("workflow.refresh")}</Button>
      </Space>
      <Table
        loading={loading}
        dataSource={rows}
        pagination={pagination}
        rowKey="id"
        columns={columns}
      />
      <Modal
        open={pending !== null}
        title={
          pending?.decision === "APPROVE"
            ? t("workflow.decision.approveTitle")
            : t("workflow.decision.rejectTitle")
        }
        onCancel={() => setPending(null)}
        onOk={() => void onDecision()}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item
            name="note"
            label={t("workflow.decision.noteLabel")}
            rules={[
              { max: 500 },
              ...(pending?.decision === "REJECT"
                ? [{ required: true, message: t("workflow.decision.rejectNoteRequired") }]
                : []),
            ]}
          >
            <Input.TextArea rows={4} placeholder={t("workflow.decision.notePlaceholder")} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
