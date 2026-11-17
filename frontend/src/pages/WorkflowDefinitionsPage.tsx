/** P5 工作流**定义管理面**（admin-only）：新建 / 编辑 / 发布 / 停用定义（模板）。
 *
 * 背景：定义原先只能直接 INSERT 落库 —— 绕过全部校验（步骤可为空、审批矩阵可缺、
 * 域/能力/版本可重号）。本页 + `/workflow/admin/definitions` 把这条写路径收回到
 * 「先校验、再落库、留审计」的面上来。
 *
 * 三条产品口径（与后端 `workflow_definition_service` 一一对应）：
 * 1. **只有 DRAFT 可编辑**：PUBLISHED 的审批矩阵在发起审批时实时读取 ⇒ 改它等于
 *    改「谁批」⇒ 已发布定义只能停用 + 新建版本。
 * 2. **发布 = 可运行性校验**：有审批门却没有匹配 (workflow_instance, 能力代码,
 *    风险级别) 的矩阵行 ⇒ 永远选不出审批人 ⇒ 发布直接 422。
 * 3. **审批矩阵的匹配键由页面派生**（对象类型 / 能力代码 / 风险级别）⇒ 前台造不出
 *    「有矩阵但选不中」的死定义。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  InputNumber,
  message,
  Modal,
  Select,
  Space,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";
import { useTablePagination } from "../utils/useTablePagination";
import {
  createWorkflowDefinition,
  disableWorkflowDefinition,
  listWorkflowDefinitionAdmin,
  publishWorkflowDefinition,
  updateWorkflowDefinition,
} from "../api/workflow";
import type {
  WorkflowApprovalMatrixEntry,
  WorkflowDefinitionAdminRead,
  WorkflowDefinitionBody,
  WorkflowDefinitionStatus,
  WorkflowDefinitionStep,
} from "../types/workflow";

/** 实例级审批的 subjectType：运行时按它 + 能力代码 + 风险级别精确匹配矩阵行。 */
const SUBJECT_TYPE_INSTANCE = "workflow_instance";

/** 风险级别候选（后端只要求非空，这里给常见三档；库里已有别的值时保留原值）。 */
const RISK_LEVELS = ["LOW", "MEDIUM", "HIGH"];

const DEFINITION_STATUSES: WorkflowDefinitionStatus[] = [
  "DRAFT",
  "PUBLISHED",
  "DISABLED",
];

const STATUS_COLORS: Record<string, string> = {
  DRAFT: "gold",
  PUBLISHED: "green",
  DISABLED: "default",
};

interface StepFormValue {
  code?: string;
  commandType?: string;
  requiresApproval?: boolean;
}

interface MatrixFormValue {
  matrixCode?: string;
  approverRole?: string;
  approverUser?: string;
  timeoutHours?: number | null;
}

interface DefinitionFormValues {
  domainCode?: string;
  capabilityCode?: string;
  version?: string;
  riskLevel?: string;
  steps?: StepFormValue[];
  matrix?: MatrixFormValue[];
}

function emptyStep(): StepFormValue {
  return { code: "", commandType: "", requiresApproval: false };
}

function emptyMatrix(): MatrixFormValue {
  return { matrixCode: "", approverRole: "", approverUser: "" };
}

/** 新建时的表单初值：一行步骤 + 一行审批矩阵（都还是空的）。 */
function emptyFormValues(): DefinitionFormValues {
  return {
    domainCode: "",
    capabilityCode: "",
    version: "",
    riskLevel: "MEDIUM",
    steps: [emptyStep()],
    matrix: [emptyMatrix()],
  };
}

/** 已有定义 → 表单初值（编辑态回填）。 */
function toFormValues(row: WorkflowDefinitionAdminRead): DefinitionFormValues {
  const body = row.definitionJson ?? { steps: [] };
  const steps = (body.steps ?? []).map((step) => ({
    code: step.code ?? "",
    commandType: step.commandType ?? "",
    requiresApproval: Boolean(step.requiresApproval),
  }));
  const matrix = (body.approvalMatrix ?? []).map((entry) => ({
    matrixCode: entry.matrixCode ?? "",
    approverRole: entry.approverRole ?? "",
    approverUser: entry.approverUser ?? "",
    timeoutHours: entry.timeoutHours ?? undefined,
  }));
  return {
    domainCode: row.domainCode,
    capabilityCode: row.capabilityCode,
    version: row.version,
    riskLevel: body.riskLevel ?? "MEDIUM",
    steps: steps.length > 0 ? steps : [emptyStep()],
    matrix,
  };
}

/**
 * 表单值 → 定义体。
 *
 * - 步骤的 `order` 用行下标：表单里的行序即执行序，后端按 order 排序；
 * - 空步骤 / 空审批行直接丢弃（用户只是开了个空行）；
 * - 审批矩阵的匹配键（subjectType / capabilityCode / riskLevel）**派生**自定义
 *   本身 ⇒ 与运行时选择器同源，不会出现「有矩阵但选不中」。
 */
function buildDefinitionJson(values: DefinitionFormValues): WorkflowDefinitionBody {
  const capabilityCode = (values.capabilityCode ?? "").trim();
  const riskLevel = (values.riskLevel ?? "").trim();
  const steps: WorkflowDefinitionStep[] = (values.steps ?? [])
    .filter((step) => (step.code ?? "").trim() !== "")
    .map((step, index) => {
      const built: WorkflowDefinitionStep = {
        code: (step.code ?? "").trim(),
        order: index,
      };
      const commandType = (step.commandType ?? "").trim();
      if (commandType !== "") built.commandType = commandType;
      if (step.requiresApproval) built.requiresApproval = true;
      return built;
    });
  const approvalMatrix: WorkflowApprovalMatrixEntry[] = (values.matrix ?? [])
    .filter((entry) => (entry.matrixCode ?? "").trim() !== "")
    .map((entry) => {
      const built: WorkflowApprovalMatrixEntry = {
        matrixCode: (entry.matrixCode ?? "").trim(),
        subjectType: SUBJECT_TYPE_INSTANCE,
        capabilityCode,
        riskLevel,
      };
      const approverRole = (entry.approverRole ?? "").trim();
      if (approverRole !== "") built.approverRole = approverRole;
      const approverUser = (entry.approverUser ?? "").trim();
      if (approverUser !== "") built.approverUser = approverUser;
      if (entry.timeoutHours !== undefined && entry.timeoutHours !== null) {
        built.timeoutHours = entry.timeoutHours;
      }
      return built;
    });
  return { riskLevel, steps, approvalMatrix };
}

export default function WorkflowDefinitionsPage() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<WorkflowDefinitionAdminRead[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string | undefined>();
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form] = Form.useForm();
  const { pagination, state, setPage } = useTablePagination({ serverTotal: total });

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const page = await listWorkflowDefinitionAdmin({
        ...(statusFilter ? { status: statusFilter as WorkflowDefinitionStatus } : {}),
        limit: state.pageSize,
        offset: (state.current - 1) * state.pageSize,
      });
      setRows(page.rows);
      setTotal(page.total);
    } catch (error: unknown) {
      // 加载失败（典型：非 admin ⇒ 403「需要 admin 角色」）⇒ 页面内提示，
      // 不弹 toast —— toast 与页面内提示会同时出现同一句文案，测试/读屏都会歧义。
      setLoadError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  }, [statusFilter, state.current, state.pageSize]);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    const values = emptyFormValues();
    form.setFieldsValue(values);
    setEditingId(null);
    setEditorOpen(true);
  };

  const openEdit = (row: WorkflowDefinitionAdminRead) => {
    form.setFieldsValue(toFormValues(row));
    setEditingId(row.id);
    setEditorOpen(true);
  };

  const onSave = async () => {
    let values: DefinitionFormValues;
    try {
      values = (await form.validateFields()) as DefinitionFormValues;
    } catch {
      // antd validateFields 校验失败时 reject；错误提示已由表单展示
      return;
    }
    const payload = {
      domainCode: (values.domainCode ?? "").trim(),
      capabilityCode: (values.capabilityCode ?? "").trim(),
      version: (values.version ?? "").trim(),
      definitionJson: buildDefinitionJson(values),
    };
    try {
      if (editingId === null) {
        await createWorkflowDefinition(payload);
      } else {
        await updateWorkflowDefinition(editingId, payload);
      }
      setEditorOpen(false);
      await load();
      void message.success(t("workflowDefinition.messages.saveOk"));
    } catch (error: unknown) {
      const detailText = error instanceof Error ? error.message : String(error);
      void message.error(t("workflowDefinition.messages.saveFailed", { detail: detailText }));
    }
  };

  const onPublish = async (definitionId: number) => {
    try {
      await publishWorkflowDefinition(definitionId);
      await load();
      void message.success(t("workflowDefinition.messages.publishOk"));
    } catch (error: unknown) {
      const detailText = error instanceof Error ? error.message : String(error);
      void message.error(t("workflowDefinition.messages.publishFailed", { detail: detailText }));
    }
  };

  const onDisable = async (definitionId: number) => {
    try {
      await disableWorkflowDefinition(definitionId);
      await load();
      void message.success(t("workflowDefinition.messages.disableOk"));
    } catch (error: unknown) {
      const detailText = error instanceof Error ? error.message : String(error);
      void message.error(t("workflowDefinition.messages.disableFailed", { detail: detailText }));
    }
  };

  const columns: ColumnsType<WorkflowDefinitionAdminRead> = [
    { title: t("workflowDefinition.columns.id"), dataIndex: "id", width: 70 },
    {
      title: t("workflowDefinition.columns.capability"),
      key: "capability",
      render: (_, row) => `${row.domainCode} / ${row.capabilityCode}`,
    },
    { title: t("workflowDefinition.columns.version"), dataIndex: "version", width: 90 },
    {
      title: t("workflowDefinition.columns.status"),
      dataIndex: "status",
      width: 100,
      render: (status: string) => (
        <Tag color={STATUS_COLORS[status]}>{t(`workflowDefinition.status.${status}`)}</Tag>
      ),
    },
    { title: t("workflowDefinition.columns.createdBy"), dataIndex: "createdBy", width: 120 },
    {
      title: t("workflowDefinition.columns.updatedTime"),
      dataIndex: "updatedTime",
      width: 180,
    },
    {
      title: t("workflowDefinition.columns.actions"),
      key: "actions",
      width: 200,
      render: (_, row) => (
        <Space>
          {row.status === "DRAFT" && (
            <>
              <Button size="small" onClick={() => openEdit(row)}>
                {t("workflowDefinition.actions.edit")}
              </Button>
              <Button size="small" type="primary" onClick={() => void onPublish(row.id)}>
                {t("workflowDefinition.actions.publish")}
              </Button>
            </>
          )}
          {row.status === "PUBLISHED" && (
            <Button size="small" danger onClick={() => void onDisable(row.id)}>
              {t("workflowDefinition.actions.disable")}
            </Button>
          )}
          {row.status === "DISABLED" && (
            <span>{t("workflowDefinition.actions.disabledHint")}</span>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div style={{ padding: 24 }}>
      <h2 style={{ marginBottom: 8 }}>{t("workflowDefinition.title")}</h2>
      <p style={{ marginBottom: 16 }}>{t("workflowDefinition.hint")}</p>
      <Space style={{ marginBottom: 16 }} wrap>
        <Button type="primary" onClick={openCreate}>
          {t("workflowDefinition.actions.create")}
        </Button>
        <Select
          allowClear
          style={{ width: 180 }}
          placeholder={t("workflowDefinition.filters.status")}
          value={statusFilter}
          onChange={(value) => {
            setStatusFilter(value);
            setPage(1);
          }}
          options={DEFINITION_STATUSES.map((status) => ({
            value: status,
            label: t(`workflowDefinition.status.${status}`),
          }))}
        />
        <Button onClick={() => void load()}>{t("workflow.refresh")}</Button>
      </Space>
      {loadError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("workflowDefinition.messages.loadFailed", { detail: loadError })}
        />
      )}
      <Table
        loading={loading}
        dataSource={rows}
        pagination={pagination}
        rowKey="id"
        columns={columns}
      />
      <DefinitionEditor
        open={editorOpen}
        editing={editingId !== null}
        form={form}
        onCancel={() => setEditorOpen(false)}
        onOk={() => void onSave()}
      />
    </div>
  );
}

interface DefinitionEditorProps {
  open: boolean;
  editing: boolean;
  form: ReturnType<typeof Form.useForm>[0];
  onCancel: () => void;
  onOk: () => void;
}

/**
 * 定义编辑弹窗。
 *
 * 刻意**不用** `destroyOnHidden`：弹窗内容一旦销毁，表单字段会注销，
 * 写进 store 的初值会被 preserve=false 的清理逻辑抹掉（打开时序不可靠）。
 * 这里保持内容常驻，初值靠 `form.setFieldsValue` 落在已注册的字段上。
 */
function DefinitionEditor({ open, editing, form, onCancel, onOk }: DefinitionEditorProps) {
  const { t } = useTranslation();
  const capabilityCode = Form.useWatch("capabilityCode", form);
  const riskLevel = Form.useWatch("riskLevel", form);
  const scope = `${SUBJECT_TYPE_INSTANCE} / ${capabilityCode ?? ""} / ${riskLevel ?? ""}`;

  const riskOptions = useMemo(() => {
    const values = [...RISK_LEVELS];
    if (riskLevel && !values.includes(riskLevel)) values.push(riskLevel);
    return values.map((value) => ({ value, label: value }));
  }, [riskLevel]);

  return (
    <Modal
      open={open}
      title={
        editing
          ? t("workflowDefinition.editor.editTitle")
          : t("workflowDefinition.editor.createTitle")
      }
      width={760}
      onCancel={onCancel}
      onOk={onOk}
    >
      <Form form={form} layout="vertical">
        <Space size="middle" style={{ display: "flex" }}>
          <Form.Item
            name="domainCode"
            label={t("workflowDefinition.editor.domainCode")}
            rules={[{ required: true }]}
            style={{ flex: 1 }}
          >
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item
            name="capabilityCode"
            label={t("workflowDefinition.editor.capabilityCode")}
            rules={[{ required: true }]}
            style={{ flex: 1 }}
          >
            <Input maxLength={64} />
          </Form.Item>
          <Form.Item
            name="version"
            label={t("workflowDefinition.editor.version")}
            rules={[{ required: true }]}
            style={{ width: 140 }}
          >
            <Input maxLength={32} />
          </Form.Item>
        </Space>
        <Form.Item
          name="riskLevel"
          label={t("workflowDefinition.editor.riskLevel")}
          rules={[{ required: true }]}
          style={{ width: 220 }}
        >
          <Select options={riskOptions} />
        </Form.Item>

        <Form.List name="steps">
          {(fields, { add, remove }) => (
            <Space direction="vertical" style={{ width: "100%" }} size="small">
              {fields.map((field) => (
                <div
                  key={field.key}
                  style={{
                    border: "1px solid #f0f0f0",
                    borderRadius: 6,
                    padding: 12,
                  }}
                >
                  <Space size="small" align="start" wrap>
                    <Form.Item
                      name={[field.name, "code"]}
                      label={t("workflowDefinition.editor.stepCode")}
                      rules={[{ required: true }]}
                      style={{ marginBottom: 8 }}
                    >
                      <Input maxLength={64} />
                    </Form.Item>
                    <Form.Item
                      name={[field.name, "commandType"]}
                      label={t("workflowDefinition.editor.commandType")}
                      style={{ marginBottom: 8 }}
                    >
                      <Input maxLength={64} />
                    </Form.Item>
                    <Form.Item
                      name={[field.name, "requiresApproval"]}
                      label={t("workflowDefinition.editor.requiresApproval")}
                      valuePropName="checked"
                      style={{ marginBottom: 8 }}
                    >
                      <Checkbox />
                    </Form.Item>
                    <Button
                      size="small"
                      danger
                      onClick={() => remove(field.name)}
                      style={{ marginTop: 30 }}
                    >
                      {t("workflowDefinition.actions.removeStep")}
                    </Button>
                  </Space>
                </div>
              ))}
              <Button size="small" onClick={() => add(emptyStep())}>
                {t("workflowDefinition.actions.addStep")}
              </Button>
            </Space>
          )}
        </Form.List>

        <Form.List name="matrix">
          {(fields, { add, remove }) => (
            <Space direction="vertical" style={{ width: "100%" }} size="small">
              <Form.Item
                label={t("workflowDefinition.editor.matrixScope")}
                tooltip={t("workflowDefinition.editor.matrixScopeHint")}
              >
                <Input value={scope} readOnly disabled />
              </Form.Item>
              {fields.map((field) => (
                <Space key={field.key} size="small" align="start" wrap>
                  <Form.Item
                    name={[field.name, "matrixCode"]}
                    label={t("workflowDefinition.editor.matrixCode")}
                    rules={[
                      {
                        validator: async (_, value: string | undefined) => {
                          const row = form.getFieldValue([
                            "matrix",
                            field.name,
                          ]) as MatrixFormValue | undefined;
                          const hasApprover =
                            (row?.approverRole ?? "") !== "" ||
                            (row?.approverUser ?? "") !== "" ||
                            (row?.timeoutHours ?? undefined) !== undefined;
                          if (!value && !hasApprover) return;
                          if (!value) {
                            throw new Error(
                              t("workflowDefinition.validation.matrixCodeRequired"),
                            );
                          }
                        },
                      },
                    ]}
                  >
                    <Input maxLength={64} />
                  </Form.Item>
                  <Form.Item
                    name={[field.name, "approverRole"]}
                    label={t("workflowDefinition.editor.approverRole")}
                  >
                    <Input maxLength={64} />
                  </Form.Item>
                  <Form.Item
                    name={[field.name, "approverUser"]}
                    label={t("workflowDefinition.editor.approverUser")}
                  >
                    <Input maxLength={64} />
                  </Form.Item>
                  <Form.Item
                    name={[field.name, "timeoutHours"]}
                    label={t("workflowDefinition.editor.timeoutHours")}
                  >
                    <InputNumber min={1} max={720} style={{ width: 120 }} />
                  </Form.Item>
                  <Button
                    size="small"
                    danger
                    onClick={() => remove(field.name)}
                    style={{ marginTop: 30 }}
                  >
                    {t("workflowDefinition.actions.removeMatrix")}
                  </Button>
                </Space>
              ))}
              <Button size="small" onClick={() => add(emptyMatrix())}>
                {t("workflowDefinition.actions.addMatrix")}
              </Button>
            </Space>
          )}
        </Form.List>
      </Form>
    </Modal>
  );
}
