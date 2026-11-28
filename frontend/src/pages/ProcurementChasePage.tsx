/** 催货单管理页（feat/delivery-chase-closed-loop）。
 *
 * 与 `ProcurementRfqPage` 同构：列表 / 详情（行 + 决策时间线）/ 操作链
 * （validate → approve/reject → send / cancel）。admin-only（对外动作）。
 */

import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Descriptions,
  Form,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";
import { useAuthStore } from "../stores/authStore";
import {
  decideChase,
  downloadChaseDraft,
  getChaseDraft,
  listChaseDrafts,
  sendChaseDraft,
  validateChaseDraft,
  type ChaseDecision,
  type ChaseDraft,
  type ChaseDraftDetail,
  type ChaseExportFormat,
  type ChaseLine,
} from "../api/procurementChase";

const STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  VALIDATING: "processing",
  NEEDS_CORRECTION: "warning",
  PENDING_APPROVAL: "processing",
  READY_TO_SEND: "cyan",
  REJECTED: "error",
  CANCELLED: "default",
  SENDING: "processing",
  SENT: "success",
  FAILED: "error",
  CLOSED: "default",
};

const DECISION_COLOR: Record<string, string> = {
  SUBMIT: "processing",
  APPROVE: "success",
  REJECT: "error",
  CANCEL: "default",
  SEND: "cyan",
};

interface CreateFormValues {
  note: string;
}

export default function ProcurementChasePage() {
  const { t } = useTranslation();
  const user = useAuthStore((s) => s.user);
  const isAdmin = (user?.roles ?? []).includes("admin");
  const [drafts, setDrafts] = useState<ChaseDraft[]>([]);
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState<ChaseDraftDetail | null>(null);
  const [noteModal, setNoteModal] = useState<{
    title: string;
    action: () => Promise<void>;
  } | null>(null);
  const [noteForm] = Form.useForm<CreateFormValues>();

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setDrafts(await listChaseDrafts());
    } catch {
      // interceptor 已提示
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const openDetail = useCallback(
    async (chaseDraftId: string) => {
      try {
        setDetail(await getChaseDraft(chaseDraftId));
      } catch {
        // interceptor 已提示
      }
    },
    [],
  );

  const runAction = useCallback(
    async (fn: () => Promise<unknown>, successText: string) => {
      try {
        await fn();
        void message.success(successText);
        setDetail(null);
        await reload();
      } catch {
        // interceptor 已提示
      }
    },
    [reload],
  );

  const askNote = useCallback(
    (title: string, action: (note: string) => Promise<void>) => {
      noteForm.resetFields();
      setNoteModal({
        title,
        action: async () => {
          const values = await noteForm.validateFields();
          await action(values.note ?? "");
        },
      });
    },
    [noteForm],
  );

  /**
   * 下载催货单（Excel 明细表 / Word 催货函）。
   *
   * 失败提示取服务端文案（两种错误包络兼容，解析在 api 层完成）；
   * 解析不出文案时才落回通用兜底 ⇒ 用户至少知道「下载失败」而不是静默无反应。
   */
  const handleExport = useCallback(
    async (chaseDraftId: string, format: ChaseExportFormat) => {
      try {
        await downloadChaseDraft(chaseDraftId, format);
        void message.success(t("chase.export.success"));
      } catch (error) {
        const text = error instanceof Error ? error.message : "";
        void message.error(text || t("chase.export.failed"));
      }
    },
    [t],
  );

  const lineColumns: ColumnsType<ChaseLine> = [
    { title: t("chase.line.poNo"), dataIndex: "poNo", width: 140 },
    { title: t("chase.line.poLine"), dataIndex: "poLine", width: 70 },
    { title: t("chase.line.material"), render: (_, line) => line.materialDesc ?? line.materialCode ?? "—" },
    { title: t("chase.line.openQty"), dataIndex: "openQty", width: 90 },
    { title: t("chase.line.expectedDate"), dataIndex: "expectedDate", width: 110 },
    { title: t("chase.line.overdueDays"), dataIndex: "overdueDays", width: 90 },
    {
      title: t("chase.line.status"),
      width: 190,
      render: (_: unknown, line: ChaseLine) =>
        line.isDirty ? (
          <Tag color="red" data-testid="chase-line-dirty">
            {t("chase.line.dirty")}
          </Tag>
        ) : (
          <Tag color="green">{t("chase.line.normal")}</Tag>
        ),
    },
  ];

  const decisionColumns: ColumnsType<ChaseDecision> = [
    {
      title: t("chase.decision.decision"),
      dataIndex: "decision",
      width: 90,
      render: (value: string) => <Tag color={DECISION_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    { title: t("chase.decision.actor"), dataIndex: "actor", width: 120 },
    { title: t("chase.decision.note"), dataIndex: "note" },
    {
      title: t("chase.decision.decidedTime"),
      dataIndex: "decidedTime",
      width: 170,
      render: (value: string | null) => (value ? new Date(value).toLocaleString() : "—"),
    },
  ];

  const statusActions = detail ? detail.draft.status : null;
  // 欠交量异常的行（红色标识，要求重点核对）
  const dirtyLineCount = detail
    ? detail.lines.filter((line) => line.isDirty).length
    : 0;

  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Button onClick={() => void reload()}>{t("common.refresh")}</Button>
      </Space>
      <Table<ChaseDraft>
        rowKey="chaseDraftId"
        size="small"
        loading={loading}
        dataSource={drafts}
        pagination={{ pageSize: 20 }}
        columns={[
          { title: t("chase.column.id"), dataIndex: "chaseDraftId", width: 300, render: (v: string) => v.slice(0, 8) },
          { title: t("chase.column.status"), dataIndex: "status", width: 140,
            render: (v: string) => <Tag color={STATUS_COLOR[v] ?? "default"}>{v}</Tag> },
          { title: t("chase.column.supplier"), dataIndex: "supplierCode", width: 120 },
          { title: t("chase.column.lines"), dataIndex: "lineCount", width: 80 },
          { title: t("chase.column.request"), dataIndex: "requestId" },
          { title: t("chase.column.createdBy"), dataIndex: "createdBy", width: 120 },
          {
            title: t("common.action"),
            width: 220,
            render: (_, record) => (
              <Space size={4}>
                <Button
                  size="small"
                  onClick={() => void handleExport(record.chaseDraftId, "xlsx")}
                >
                  {t("chase.export.excel")}
                </Button>
                <Button
                  size="small"
                  onClick={() => void handleExport(record.chaseDraftId, "docx")}
                >
                  {t("chase.export.word")}
                </Button>
                <a onClick={() => void openDetail(record.chaseDraftId)}>
                  {t("chase.column.detail")}
                </a>
              </Space>
            ),
          },
        ]}
      />

      <Modal
        title={t("chase.detail.title")}
        open={detail !== null}
        onCancel={() => setDetail(null)}
        footer={
          isAdmin && detail ? (
            <Space>
              {statusActions === "DRAFT" && (
                <Button
                  type="primary"
                  onClick={() =>
                    void runAction(
                      () => validateChaseDraft(detail.draft.chaseDraftId),
                      t("chase.action.validated"),
                    )
                  }
                >
                  {t("chase.action.validate")}
                </Button>
              )}
              {statusActions === "PENDING_APPROVAL" && (
                <>
                  <Button
                    type="primary"
                    onClick={() =>
                      void runAction(
                        () => decideChase(detail.draft.chaseDraftId, "approve"),
                        t("chase.action.approved"),
                      )
                    }
                  >
                    {t("chase.action.approve")}
                  </Button>
                  <Button
                    danger
                    onClick={() =>
                      askNote(t("chase.action.reject"), (note) =>
                        runAction(
                          () => decideChase(detail.draft.chaseDraftId, "reject", note),
                          t("chase.action.rejected"),
                        ),
                      )
                    }
                  >
                    {t("chase.action.reject")}
                  </Button>
                </>
              )}
              {statusActions === "READY_TO_SEND" && (
                <>
                  <Button
                    type="primary"
                    onClick={() =>
                      void runAction(
                        () => sendChaseDraft(detail.draft.chaseDraftId),
                        t("chase.action.sent"),
                      )
                    }
                  >
                    {t("chase.action.send")}
                  </Button>
                  <Button
                    onClick={() =>
                      askNote(t("chase.action.cancel"), (note) =>
                        runAction(
                          () => decideChase(detail.draft.chaseDraftId, "cancel", note),
                          t("chase.action.cancelled"),
                        ),
                      )
                    }
                  >
                    {t("chase.action.cancel")}
                  </Button>
                </>
              )}
            </Space>
          ) : null
        }
        width={900}
      >
        {detail && (
          <>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label={t("chase.detail.status")}>
                <Tag color={STATUS_COLOR[detail.draft.status] ?? "default"}>
                  {detail.draft.status}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t("chase.detail.supplier")}>
                {detail.draft.supplierCode}
              </Descriptions.Item>
              <Descriptions.Item label={t("chase.detail.deadline")}>
                {new Date(detail.draft.expectedReplyDeadline).toLocaleString()}
              </Descriptions.Item>
              <Descriptions.Item label={t("chase.detail.request")}>
                {detail.draft.requestId}
              </Descriptions.Item>
            </Descriptions>
            <h4 style={{ marginTop: 16 }}>
              {t("chase.detail.lines")}
              {dirtyLineCount > 0 && (
                <Tag color="red" style={{ marginLeft: 8 }}>
                  {t("chase.detail.dirtyLines", { count: String(dirtyLineCount) })}
                </Tag>
              )}
            </h4>
            <Table<ChaseLine>
              rowKey={(line) => `${line.poNo}-${line.poLine}`}
              size="small"
              pagination={false}
              dataSource={detail.lines}
              columns={lineColumns}
            />
            <h4 style={{ marginTop: 16 }}>{t("chase.detail.decisions")}</h4>
            <Table<ChaseDecision>
              rowKey={(d) => `${d.decision}-${d.decidedTime}`}
              size="small"
              pagination={false}
              dataSource={detail.decisions}
              columns={decisionColumns}
            />
          </>
        )}
      </Modal>

      <Modal
        title={noteModal?.title}
        open={noteModal !== null}
        onCancel={() => setNoteModal(null)}
        onOk={async () => {
          if (!noteModal) return;
          try {
            await noteModal.action();
            void message.success(t("chase.action.done"));
            setNoteModal(null);
            setDetail(null);
            await reload();
          } catch {
            // interceptor 已提示
          }
        }}
      >
        <Form form={noteForm} layout="vertical">
          <Form.Item name="note" label={t("chase.action.note")}>
            <Input.TextArea rows={3} maxLength={2000} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

