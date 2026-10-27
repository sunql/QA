import { useEffect, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Form,
  Modal,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import type { TableColumnsType } from "antd";
import { useTranslation } from "../i18n";
import { listAgents } from "../api/agentRegistry";
import {
  createOrchestration,
  listOrchestrations,
  listOrchestrationToolOptions,
  toggleOrchestration,
  updateOrchestration,
} from "../api/agentOrchestrationApi";
import OrchestrationProfileForm, {
  DEFAULT_PROFILE_VALUES,
} from "../components/agent/OrchestrationProfileForm";
import type { ProfileFormValues } from "../components/agent/OrchestrationProfileForm";
import type { AgentDefinition } from "../types/agentRegistry";
import type {
  AgentOrchestrationProfileRead,
  AgentOrchestrationToolOptionRead,
} from "../types/agentOrchestration";

/** 后端把 axios 错误归一化成 AppError{code,status,detail}，没有 .response。 */
interface StatusError extends Error {
  status?: number;
}

/** 绑定工具按 sortOrder 排序后的名称列表（不修改入参）。 */
function orderedToolNames(profile: AgentOrchestrationProfileRead): string[] {
  return [...profile.bindings]
    .sort((left, right) => left.sortOrder - right.sortOrder)
    .map((binding) => binding.toolName);
}

/** 创建/更新请求共用的字段（更新时再加 revision）。 */
interface ProfilePayload {
  instructions: string;
  toolNames: string[];
  defaultMaxIterations: number;
  defaultMaxToolCalls: number;
  defaultMaxCostUsd: number;
  defaultTimeoutSeconds: number;
  maxObservationChars: number;
}

/** 从表单取值构造创建/更新共用的字段。 */
function buildProfilePayload(values: ProfileFormValues): ProfilePayload {
  return {
    instructions: values.instructions,
    toolNames: values.toolNames ?? [],
    defaultMaxIterations:
      values.defaultMaxIterations ?? DEFAULT_PROFILE_VALUES.defaultMaxIterations,
    defaultMaxToolCalls:
      values.defaultMaxToolCalls ?? DEFAULT_PROFILE_VALUES.defaultMaxToolCalls,
    defaultMaxCostUsd:
      values.defaultMaxCostUsd ?? DEFAULT_PROFILE_VALUES.defaultMaxCostUsd,
    defaultTimeoutSeconds:
      values.defaultTimeoutSeconds ??
      DEFAULT_PROFILE_VALUES.defaultTimeoutSeconds,
    maxObservationChars:
      values.maxObservationChars ?? DEFAULT_PROFILE_VALUES.maxObservationChars,
  };
}

export default function AgentOrchestrationPage() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // t 每次渲染都是新闭包：放进 effect 依赖会触发无限重渲染，仓库统一用 ref 取最新值。
  const tRef = useRef(t);
  tRef.current = t;

  const [form] = Form.useForm<ProfileFormValues>();
  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [toolOptions, setToolOptions] = useState<
    AgentOrchestrationToolOptionRead[]
  >([]);
  const [profiles, setProfiles] = useState<AgentOrchestrationProfileRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<AgentOrchestrationProfileRead | null>(
    null,
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    const agentsPromise = listAgents().then(
      (rows) => {
        if (!cancelled) setAgents(rows);
      },
      () => {
        if (!cancelled)
          setPageError(tRef.current("agentOrchestration.messages.loadAgentsFailed"));
      },
    );
    const toolsPromise = listOrchestrationToolOptions().then(
      (rows) => {
        if (!cancelled) setToolOptions(rows);
      },
      () => {
        if (!cancelled)
          setPageError(
            tRef.current("agentOrchestration.messages.loadToolsFailed"),
          );
      },
    );
    const profilesPromise = listOrchestrations().then(
      (rows) => {
        if (!cancelled) setProfiles(rows);
      },
      () => {
        if (!cancelled)
          setPageError(
            tRef.current("agentOrchestration.messages.loadProfilesFailed"),
          );
      },
    );

    void Promise.all([agentsPromise, toolsPromise, profilesPromise]).then(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  /** 写操作的错误提示：409 是乐观锁冲突，单独给友好文案。 */
  function describeFailure(error: unknown, failureKey: string): string {
    const status = (error as StatusError).status;
    if (status === 409) return t("agentOrchestration.messages.conflict");
    return t(failureKey);
  }

  function describeCreateFailure(error: unknown): string {
    const status = (error as StatusError).status;
    if (status === 409) return t("agentOrchestration.messages.duplicate");
    return t("agentOrchestration.messages.createFailed");
  }

  const openCreate = (): void => {
    setEditing(null);
    setSaveError(null);
    form.resetFields();
    form.setFieldsValue({ ...DEFAULT_PROFILE_VALUES, toolNames: [] });
    setModalOpen(true);
  };

  const openEdit = (profile: AgentOrchestrationProfileRead): void => {
    setEditing(profile);
    setSaveError(null);
    form.resetFields();
    form.setFieldsValue({
      agentCode: profile.agentCode,
      instructions: profile.instructions,
      toolNames: orderedToolNames(profile),
      defaultMaxIterations: profile.defaultMaxIterations,
      defaultMaxToolCalls: profile.defaultMaxToolCalls,
      defaultMaxCostUsd: profile.defaultMaxCostUsd,
      defaultTimeoutSeconds: profile.defaultTimeoutSeconds,
      maxObservationChars: profile.maxObservationChars,
    });
    setModalOpen(true);
  };

  const handleSubmit = async (): Promise<void> => {
    let values: ProfileFormValues;
    try {
      values = await form.validateFields();
    } catch {
      // 校验失败：antd 已在字段下方提示，这里直接返回，不产生未处理的 rejection。
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      if (editing) {
        const updated = await updateOrchestration(editing.agentCode, {
          ...buildProfilePayload(values),
          revision: editing.revision,
        });
        setProfiles((previous) =>
          previous.map((profile) =>
            profile.agentCode === updated.agentCode ? updated : profile,
          ),
        );
        message.success(t("agentOrchestration.messages.updated"));
      } else {
        const created = await createOrchestration({
          agentCode: values.agentCode,
          ...buildProfilePayload(values),
        });
        setProfiles((previous) => [
          ...previous.filter((profile) => profile.agentCode !== created.agentCode),
          created,
        ]);
        message.success(t("agentOrchestration.messages.created"));
      }
      setModalOpen(false);
    } catch (error: unknown) {
      setSaveError(
        editing
          ? describeFailure(error, "agentOrchestration.messages.updateFailed")
          : describeCreateFailure(error),
      );
    } finally {
      setSaving(false);
    }
  };

  const handleToggle = async (
    profile: AgentOrchestrationProfileRead,
  ): Promise<void> => {
    setPageError(null);
    try {
      const updated = await toggleOrchestration(profile.agentCode, {
        enabled: !profile.enabled,
        revision: profile.revision,
      });
      setProfiles((previous) =>
        previous.map((item) =>
          item.agentCode === updated.agentCode ? updated : item,
        ),
      );
      message.success(t("agentOrchestration.messages.toggled"));
    } catch (error: unknown) {
      setPageError(
        describeFailure(error, "agentOrchestration.messages.toggleFailed"),
      );
    }
  };

  const agentChoices = agents.map((agent) => ({
    value: agent.agentCode,
    label: agent.agentCode,
  }));
  const toolChoices = toolOptions.map((option) => ({
    value: option.toolName,
    label: option.toolName,
  }));

  const columns: TableColumnsType<AgentOrchestrationProfileRead> = [
    {
      title: t("agentOrchestration.columns.agentCode"),
      dataIndex: "agentCode",
      key: "agentCode",
    },
    {
      title: t("agentOrchestration.columns.tools"),
      key: "tools",
      render: (_value, record) =>
        t("agentOrchestration.toolCount", { count: record.bindings.length }),
    },
    {
      title: t("agentOrchestration.columns.enabled"),
      key: "enabled",
      render: (_value, record) => (
        <Space>
          <Switch
            checked={record.enabled}
            aria-label={t("agentOrchestration.columns.enabled")}
            onChange={() => void handleToggle(record)}
          />
          <Tag color={record.enabled ? "green" : "default"}>
            {record.enabled
              ? t("agentOrchestration.status.enabled")
              : t("agentOrchestration.status.disabled")}
          </Tag>
        </Space>
      ),
    },
    {
      title: t("agentOrchestration.columns.revision"),
      dataIndex: "revision",
      key: "revision",
    },
    {
      title: t("agentOrchestration.columns.actions"),
      key: "actions",
      render: (_value, record) => (
        <Button size="small" onClick={() => openEdit(record)}>
          {t("agentOrchestration.editButton")}
        </Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card size="small">
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <Typography.Title level={4} style={{ margin: 0 }}>
            {t("agentOrchestration.title")}
          </Typography.Title>
          <Typography.Paragraph type="secondary">
            {t("agentOrchestration.subtitle")}
          </Typography.Paragraph>
          <Button type="primary" onClick={openCreate}>
            {t("agentOrchestration.createButton")}
          </Button>
        </Space>
      </Card>

      {pageError ? <Alert type="error" showIcon message={pageError} /> : null}

      <Card size="small">
        <Table<AgentOrchestrationProfileRead>
          rowKey="agentCode"
          columns={columns}
          dataSource={profiles}
          loading={loading}
          pagination={false}
          locale={{ emptyText: t("agentOrchestration.empty.profiles") }}
        />
      </Card>

      <Modal
        open={modalOpen}
        forceRender
        width={720}
        title={
          editing
            ? t("agentOrchestration.editTitle")
            : t("agentOrchestration.createTitle")
        }
        okText={
          editing
            ? t("agentOrchestration.editSubmit")
            : t("agentOrchestration.createSubmit")
        }
        cancelText={t("agentOrchestration.cancelButton")}
        confirmLoading={saving}
        onOk={() => void handleSubmit()}
        onCancel={() => setModalOpen(false)}
      >
        {saveError ? <Alert type="error" showIcon message={saveError} /> : null}
        <OrchestrationProfileForm
          form={form}
          agentChoices={agentChoices}
          toolChoices={toolChoices}
          isEditing={editing !== null}
        />
      </Modal>
    </Space>
  );
}
