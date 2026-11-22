/** G1：语义发布管理页（编译器设计稿 §8 的前端缺口）。
 *
 * 为什么要有这一页：发布/激活/回滚此前只能走 API 或脚本——运维看不到校验详情、
 * 点不了回滚。本页把「草稿→校验→激活→归档→回滚」搬进浏览器。
 *
 * 口径（与后端一致）：
 * - 整组接口 **admin-only**（后端 `requireAdmin`）；非 admin 只读展示列表，隐藏写操作；
 * - `DRAFT` 允许带 **blockers** 落库（评审要看被拦下的内容），但**激活**时阻断项
 *   非空会被后端拒绝——页面上激活前显式提示；
 * - 回滚是**副本式**：要填一个**新**版本号（不复用旧号）。
 */

import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "../i18n";
import { useAuthStore } from "../stores/authStore";
import {
  activateSemanticRelease,
  archiveSemanticRelease,
  createSemanticReleaseDraft,
  getSemanticRelease,
  listSemanticReleaseChecks,
  listSemanticReleases,
  rollbackSemanticRelease,
  validateSemanticRelease,
  type SemanticReleaseCheck,
  type SemanticReleaseSnapshot,
  type SemanticReleaseSummary,
} from "../api/semanticRelease";

const { Text, Paragraph } = Typography;

const STATUS_COLOR: Record<string, string> = {
  DRAFT: "default",
  ACTIVE: "success",
  ARCHIVED: "processing",
};
const SEVERITY_COLOR: Record<string, string> = {
  ERROR: "error",
  WARNING: "warning",
  INFO: "default",
};
const RESULT_COLOR: Record<string, string> = {
  PASS: "success",
  FAIL: "error",
  SKIPPED: "default",
};

export default function SemanticReleasePage() {
  const { t } = useTranslation();
  const user = useAuthStore((state) => state.user);
  const isAdmin = (user?.roles ?? []).includes("admin");

  const [rows, setRows] = useState<SemanticReleaseSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState<string | undefined>();
  const [domainFilter, setDomainFilter] = useState("");
  const [page, setPage] = useState({ current: 1, pageSize: 20 });

  const [detail, setDetail] = useState<SemanticReleaseSnapshot | null>(null);
  const [checks, setChecks] = useState<SemanticReleaseCheck[]>([]);
  const [checksLoading, setChecksLoading] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);

  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();
  const [rollbackTarget, setRollbackTarget] = useState<SemanticReleaseSummary | null>(null);
  const [rollbackForm] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await listSemanticReleases({
        status: statusFilter,
        domainCode: domainFilter.trim() || undefined,
        limit: page.pageSize,
        offset: (page.current - 1) * page.pageSize,
      });
      setRows(result.rows);
      setTotal(result.total);
    } catch {
      // interceptor 已提示
    } finally {
      setLoading(false);
    }
  }, [statusFilter, domainFilter, page]);

  useEffect(() => {
    void load();
  }, [load]);

  const openDetail = async (record: SemanticReleaseSummary) => {
    setChecksLoading(true);
    try {
      const [snapshot, checkPage] = await Promise.all([
        getSemanticRelease(record.id),
        listSemanticReleaseChecks(record.id),
      ]);
      setDetail(snapshot);
      setChecks(checkPage.rows);
    } catch {
      // interceptor 已提示
    } finally {
      setChecksLoading(false);
    }
  };

  const runOperation = async (id: number, action: () => Promise<{ warnings: string[]; blockers: string[] }>) => {
    setBusyId(id);
    try {
      const result = await action();
      const warn = result.warnings.length;
      message.success(
        warn ? t("semanticRelease.opSuccessWithWarnings", { count: warn }) : t("semanticRelease.opSuccess"),
      );
      void load();
      if (detail?.id === id) void openDetail(detail);
    } catch {
      // interceptor 已提示（阻断项非空时后端返回 409/422，详情在错误里）
    } finally {
      setBusyId(null);
    }
  };

  const columns: ColumnsType<SemanticReleaseSummary> = [
    { title: t("semanticRelease.version"), dataIndex: "version", width: 190 },
    {
      title: t("semanticRelease.domain"),
      dataIndex: "domainCode",
      width: 130,
      render: (value: string | null) => value ?? t("semanticRelease.globalScope"),
    },
    {
      title: t("semanticRelease.status"),
      dataIndex: "status",
      width: 100,
      render: (value: string) => <Tag color={STATUS_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    { title: t("semanticRelease.schemaVersion"), dataIndex: "schemaVersion", width: 110 },
    {
      title: t("semanticRelease.manifestHash"),
      dataIndex: "manifestHash",
      width: 140,
      render: (value: string) => <Text code>{String(value).slice(0, 12)}…</Text>,
    },
    { title: t("semanticRelease.activatedTime"), dataIndex: "activatedTime", width: 180, render: (value: string | null) => value ?? "—" },
    {
      title: t("semanticRelease.actions"),
      width: 300,
      render: (_, record) => (
        <Space wrap>
          <Button size="small" onClick={() => void openDetail(record)}>
            {t("semanticRelease.detail")}
          </Button>
          {isAdmin && record.status === "DRAFT" && (
            <>
              <Button
                size="small"
                loading={busyId === record.id}
                onClick={() => void runOperation(record.id, () => validateSemanticRelease(record.id))}
              >
                {t("semanticRelease.validate")}
              </Button>
              <Popconfirm
                title={t("semanticRelease.activateConfirm")}
                onConfirm={() => void runOperation(record.id, () => activateSemanticRelease(record.id))}
              >
                <Button size="small" type="primary" loading={busyId === record.id}>
                  {t("semanticRelease.activate")}
                </Button>
              </Popconfirm>
            </>
          )}
          {isAdmin && record.status === "ACTIVE" && (
            <Popconfirm
              title={t("semanticRelease.archiveConfirm")}
              onConfirm={() => void runOperation(record.id, () => archiveSemanticRelease(record.id))}
            >
              <Button size="small" loading={busyId === record.id}>
                {t("semanticRelease.archive")}
              </Button>
            </Popconfirm>
          )}
          {isAdmin && (
            <Button size="small" onClick={() => setRollbackTarget(record)}>
              {t("semanticRelease.rollback")}
            </Button>
          )}
        </Space>
      ),
    },
  ];

  const checkColumns: ColumnsType<SemanticReleaseCheck> = [
    { title: t("semanticRelease.checkCode"), dataIndex: "checkCode", width: 240 },
    {
      title: t("semanticRelease.severity"),
      dataIndex: "severity",
      width: 110,
      render: (value: string) => <Tag color={SEVERITY_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    {
      title: t("semanticRelease.result"),
      dataIndex: "result",
      width: 110,
      render: (value: string) => <Tag color={RESULT_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    {
      title: t("semanticRelease.checkDetail"),
      dataIndex: "detail",
      render: (value: Record<string, unknown> | null) =>
        value ? <Text code>{JSON.stringify(value).slice(0, 160)}</Text> : "—",
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <h2>{t("semanticRelease.title")}</h2>
      {!isAdmin && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 12 }}
          message={t("semanticRelease.adminOnly")}
        />
      )}
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space wrap>
          <Select
            allowClear
            style={{ width: 160 }}
            placeholder={t("semanticRelease.status")}
            value={statusFilter}
            onChange={(value) => {
              setStatusFilter(value);
              setPage((prev) => ({ ...prev, current: 1 }));
            }}
            options={["DRAFT", "ACTIVE", "ARCHIVED"].map((value) => ({ value, label: value }))}
          />
          <Input
            style={{ width: 200 }}
            placeholder={t("semanticRelease.domainPlaceholder")}
            value={domainFilter}
            onChange={(event) => {
              setDomainFilter(event.target.value);
              setPage((prev) => ({ ...prev, current: 1 }));
            }}
          />
          <Button onClick={() => void load()}>{t("common.refresh")}</Button>
          {isAdmin && (
            <Button type="primary" onClick={() => setCreateOpen(true)}>
              {t("semanticRelease.createDraft")}
            </Button>
          )}
        </Space>
      </Card>

      <Table
        rowKey="id"
        loading={loading}
        dataSource={rows}
        columns={columns}
        pagination={{
          current: page.current,
          pageSize: page.pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: ["10", "20", "50"],
          showTotal: (count) => t("common.totalItems", { total: count }),
          onChange: (current, pageSize) => setPage({ current, pageSize }),
        }}
      />

      <Drawer
        title={detail ? `${detail.version} · ${detail.status}` : t("semanticRelease.detail")}
        open={detail !== null}
        width={760}
        onClose={() => setDetail(null)}
      >
        {detail && (
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label={t("semanticRelease.domain")}>
                {detail.domainCode ?? t("semanticRelease.globalScope")}
              </Descriptions.Item>
              <Descriptions.Item label={t("semanticRelease.schemaVersion")}>
                {detail.schemaVersion}
              </Descriptions.Item>
              <Descriptions.Item label={t("semanticRelease.manifestHash")} span={2}>
                <Text code>{detail.manifestHash}</Text>
              </Descriptions.Item>
              <Descriptions.Item label={t("semanticRelease.activatedTime")}>
                {detail.activatedTime ?? "—"}
              </Descriptions.Item>
              <Descriptions.Item label={t("semanticRelease.createdBy")}>
                {detail.createdBy ?? "—"}
              </Descriptions.Item>
            </Descriptions>
            <Card size="small" title={t("semanticRelease.manifestSummary")}>
              <Paragraph>
                {Object.keys(detail.manifest ?? {})
                  .map((key) => `${key}: ${Array.isArray((detail.manifest as Record<string, unknown>)[key]) ? ((detail.manifest as Record<string, unknown>)[key] as unknown[]).length : typeof (detail.manifest as Record<string, unknown>)[key]}`)
                  .join(" · ")}
              </Paragraph>
            </Card>
            <Card size="small" title={t("semanticRelease.checks")} loading={checksLoading}>
              {checks.length === 0 ? (
                <Empty description={t("semanticRelease.noChecks")} />
              ) : (
                <Table
                  rowKey="id"
                  size="small"
                  dataSource={checks}
                  columns={checkColumns}
                  pagination={{ pageSize: 10 }}
                />
              )}
            </Card>
          </Space>
        )}
      </Drawer>

      <Modal
        title={t("semanticRelease.createDraft")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={async () => {
          const values = await createForm.validateFields();
          try {
            const result = await createSemanticReleaseDraft(values);
            const blockers = result.blockers.length;
            message[blockers ? "warning" : "success"](
              blockers
                ? t("semanticRelease.draftWithBlockers", { count: blockers })
                : t("semanticRelease.opSuccess"),
            );
            setCreateOpen(false);
            createForm.resetFields();
            void load();
          } catch {
            // interceptor 已提示
          }
        }}
        destroyOnHidden
      >
        <Form form={createForm} layout="vertical">
          <Form.Item
            name="version"
            label={t("semanticRelease.version")}
            rules={[{ required: true, message: t("semanticRelease.versionRequired") }]}
            extra={t("semanticRelease.versionHint")}
          >
            <Input placeholder="2026.11.22-1" />
          </Form.Item>
          <Form.Item name="domainCode" label={t("semanticRelease.domain")} extra={t("semanticRelease.domainHint")}>
            <Input placeholder="PROCUREMENT" />
          </Form.Item>
          <Form.Item name="note" label={t("semanticRelease.note")}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t("semanticRelease.rollbackTitle", { version: rollbackTarget?.version ?? "" })}
        open={rollbackTarget !== null}
        onCancel={() => setRollbackTarget(null)}
        onOk={async () => {
          if (!rollbackTarget) return;
          const values = await rollbackForm.validateFields();
          const target = rollbackTarget;
          setRollbackTarget(null);
          rollbackForm.resetFields();
          await runOperation(target.id, () =>
            rollbackSemanticRelease(target.id, values),
          );
        }}
        destroyOnHidden
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t("semanticRelease.rollbackExplain")}
        />
        <Form form={rollbackForm} layout="vertical">
          <Form.Item
            name="version"
            label={t("semanticRelease.newVersion")}
            rules={[{ required: true, message: t("semanticRelease.versionRequired") }]}
            extra={t("semanticRelease.rollbackVersionHint")}
          >
            <Input placeholder="2026.11.22-rb1" />
          </Form.Item>
          <Form.Item name="note" label={t("semanticRelease.note")}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
