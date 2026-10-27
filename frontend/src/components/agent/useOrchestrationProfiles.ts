import { useEffect, useRef, useState } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { App, Form } from "antd";
import type { FormInstance } from "antd";
import { listAgents } from "../../api/agentRegistry";
import {
  createOrchestration,
  listOrchestrations,
  listOrchestrationToolOptions,
  toggleOrchestration,
  updateOrchestration,
} from "../../api/agentOrchestrationApi";
import { DEFAULT_PROFILE_VALUES } from "./OrchestrationProfileForm";
import type { ProfileFormValues, SelectChoice } from "./OrchestrationProfileForm";
import { errorDetailText, errorStatus } from "./task/errors";
import type { Translate } from "./task/errors";
import type { AgentDefinition } from "../../types/agentRegistry";
import type {
  AgentOrchestrationProfileCreate,
  AgentOrchestrationProfileRead,
  AgentOrchestrationToolOptionRead,
} from "../../types/agentOrchestration";

/** antd 的 message 实例类型（由 App.useApp() 提供）。 */
export type OrchestrationMessage = ReturnType<typeof App.useApp>["message"];

/** 创建/更新请求共用的字段（更新时再加 revision）。 */
export type ProfilePayload = Omit<
  AgentOrchestrationProfileCreate,
  "agentCode"
>;

/** 绑定工具按 sortOrder 排序后的名称列表（不修改入参）。 */
function orderedToolNames(profile: AgentOrchestrationProfileRead): string[] {
  return [...profile.bindings]
    .sort((left, right) => left.sortOrder - right.sortOrder)
    .map((binding) => binding.toolName);
}

/** 从表单取值构造创建/更新共用的字段。 */
function buildProfilePayload(values: ProfileFormValues): ProfilePayload {
  const defaults = DEFAULT_PROFILE_VALUES;
  return {
    instructions: values.instructions,
    toolNames: values.toolNames ?? [],
    defaultMaxIterations:
      values.defaultMaxIterations ?? defaults.defaultMaxIterations,
    defaultMaxToolCalls:
      values.defaultMaxToolCalls ?? defaults.defaultMaxToolCalls,
    defaultMaxCostUsd: values.defaultMaxCostUsd ?? defaults.defaultMaxCostUsd,
    defaultTimeoutSeconds:
      values.defaultTimeoutSeconds ?? defaults.defaultTimeoutSeconds,
    maxObservationChars:
      values.maxObservationChars ?? defaults.maxObservationChars,
  };
}

/**
 * 写操作的错误提示：按状态码分流，避免把权限/校验/找不到统统说成“失败”。
 * 422 追加后端给出的具体原因（如「工具不存在：X」「超出系统上限 20」）。
 */
function describeFailure(
  t: Translate,
  error: unknown,
  failureKey: string,
): string {
  const status = errorStatus(error);
  if (status === 409) return t("agentOrchestration.messages.conflict");
  if (status === 403) return t("agentOrchestration.messages.forbidden");
  if (status === 404) return t("agentOrchestration.messages.agentNotFound");
  if (status === 422) {
    const detail = errorDetailText(error);
    return detail
      ? t("agentOrchestration.messages.validationFailed", { detail })
      : t("agentOrchestration.messages.validationFailedGeneric");
  }
  return t(failureKey);
}

/** 创建失败：409（重名）与其它错误分开提示。 */
function describeCreateFailure(t: Translate, error: unknown): string {
  if (errorStatus(error) === 409) {
    return t("agentOrchestration.messages.duplicate");
  }
  return describeFailure(t, error, "agentOrchestration.messages.createFailed");
}

/** 首次加载时用到的 state setter。 */
interface OrchestrationDataSetters {
  setAgents: Dispatch<SetStateAction<AgentDefinition[]>>;
  setToolOptions: Dispatch<
    SetStateAction<AgentOrchestrationToolOptionRead[]>
  >;
  setProfiles: Dispatch<SetStateAction<AgentOrchestrationProfileRead[]>>;
  setPageError: Dispatch<SetStateAction<string | null>>;
  setLoading: Dispatch<SetStateAction<boolean>>;
}

/** 页面级只读数据、列表与当前编辑对象。 */
export interface OrchestrationData {
  agentChoices: SelectChoice[];
  toolChoices: SelectChoice[];
  profiles: AgentOrchestrationProfileRead[];
  loading: boolean;
  pageError: string | null;
  editing: AgentOrchestrationProfileRead | null;
  setEditing: Dispatch<SetStateAction<AgentOrchestrationProfileRead | null>>;
  setProfiles: Dispatch<SetStateAction<AgentOrchestrationProfileRead[]>>;
  setPageError: Dispatch<SetStateAction<string | null>>;
  refreshProfiles: () => Promise<void>;
}

/**
 * 首次加载：Agent 列表、只读工具选项与配置列表并行拉取。
 * 任一失败只记录页面级错误，不影响其它请求。
 */
function useInitialLoad(
  setters: OrchestrationDataSetters,
  tRef: MutableRefObject<Translate>,
): void {
  useEffect(() => {
    let cancelled = false;
    const guard = (apply: () => void): void => {
      if (!cancelled) apply();
    };
    const fail = (key: string): void => {
      guard(() => setters.setPageError(tRef.current(key)));
    };
    setters.setLoading(true);
    const agentsPromise = listAgents().then(
      (rows) => guard(() => setters.setAgents(rows)),
      () => fail("agentOrchestration.messages.loadAgentsFailed"),
    );
    const toolsPromise = listOrchestrationToolOptions().then(
      (rows) => guard(() => setters.setToolOptions(rows)),
      () => fail("agentOrchestration.messages.loadToolsFailed"),
    );
    const profilesPromise = listOrchestrations().then(
      (rows) => guard(() => setters.setProfiles(rows)),
      () => fail("agentOrchestration.messages.loadProfilesFailed"),
    );
    void Promise.all([agentsPromise, toolsPromise, profilesPromise]).then(() =>
      guard(() => setters.setLoading(false)),
    );
    return () => {
      cancelled = true;
    };
  }, []);
}

/** 重新拉取配置列表；冲突后用它把服务端最新 revision 同步回 state。 */
async function reloadProfiles(
  setProfiles: Dispatch<SetStateAction<AgentOrchestrationProfileRead[]>>,
  setEditing: Dispatch<SetStateAction<AgentOrchestrationProfileRead | null>>,
  setPageError: Dispatch<SetStateAction<string | null>>,
  t: Translate,
): Promise<void> {
  try {
    const rows = await listOrchestrations();
    setProfiles(rows);
    setEditing((current) => {
      if (!current) return current;
      return rows.find((row) => row.agentCode === current.agentCode) ?? current;
    });
  } catch {
    setPageError(t("agentOrchestration.messages.loadProfilesFailed"));
  }
}

/** 页面级数据：可选项、配置列表、加载状态与刷新。 */
export function useOrchestrationData(t: Translate): OrchestrationData {
  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [toolOptions, setToolOptions] = useState<
    AgentOrchestrationToolOptionRead[]
  >([]);
  const [profiles, setProfiles] = useState<AgentOrchestrationProfileRead[]>(
    [],
  );
  const [editing, setEditing] = useState<AgentOrchestrationProfileRead | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);
  // t 每次渲染都是新闭包：放进 effect 依赖会触发无限重渲染，仓库统一用 ref 取最新值。
  const tRef = useRef(t);
  tRef.current = t;

  useInitialLoad(
    { setAgents, setToolOptions, setProfiles, setPageError, setLoading },
    tRef,
  );

  const refreshProfiles = async (): Promise<void> =>
    reloadProfiles(setProfiles, setEditing, setPageError, t);

  return {
    agentChoices: agents.map((agent) => ({
      value: agent.agentCode,
      label: agent.agentCode,
    })),
    toolChoices: toolOptions.map((option) => ({
      value: option.toolName,
      label: option.toolName,
    })),
    profiles,
    loading,
    pageError,
    editing,
    setEditing,
    setProfiles,
    setPageError,
    refreshProfiles,
  };
}

/** 弹窗（创建 / 编辑）需要的上下文。 */
export interface ProfileModalOptions {
  t: Translate;
  message: OrchestrationMessage;
  form: FormInstance<ProfileFormValues>;
  editing: AgentOrchestrationProfileRead | null;
  setEditing: Dispatch<SetStateAction<AgentOrchestrationProfileRead | null>>;
  setProfiles: Dispatch<SetStateAction<AgentOrchestrationProfileRead[]>>;
  refreshProfiles: () => Promise<void>;
}

/** 弹窗状态与动作。 */
export interface ProfileModalState {
  saving: boolean;
  saveError: string | null;
  modalOpen: boolean;
  openCreate: () => void;
  openEdit: (profile: AgentOrchestrationProfileRead) => void;
  closeModal: () => void;
  handleSubmit: () => Promise<void>;
}

/** 提交表单需要的上下文。 */
interface ProfileSubmitOptions {
  t: Translate;
  message: OrchestrationMessage;
  form: FormInstance<ProfileFormValues>;
  editing: AgentOrchestrationProfileRead | null;
  setProfiles: Dispatch<SetStateAction<AgentOrchestrationProfileRead[]>>;
  setSaveError: Dispatch<SetStateAction<string | null>>;
  setModalOpen: Dispatch<SetStateAction<boolean>>;
  setSaving: Dispatch<SetStateAction<boolean>>;
  refreshProfiles: () => Promise<void>;
}

/** 编辑态回填：把配置写进表单（Agent 编码在更新请求里不在 body 内）。 */
function fillEditForm(
  form: FormInstance<ProfileFormValues>,
  profile: AgentOrchestrationProfileRead,
): void {
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
}

/** 读取并校验表单；校验失败返回 null（antd 已在字段下方提示）。 */
async function readValidatedValues(
  form: FormInstance<ProfileFormValues>,
): Promise<ProfileFormValues | null> {
  try {
    return await form.validateFields();
  } catch {
    // 校验失败：antd 已在字段下方提示，这里直接返回，不产生未处理的 rejection。
    return null;
  }
}

/** 创建或更新一条配置：返回服务端的新配置。 */
async function saveProfile(
  editing: AgentOrchestrationProfileRead | null,
  values: ProfileFormValues,
): Promise<AgentOrchestrationProfileRead> {
  if (editing) {
    return updateOrchestration(editing.agentCode, {
      ...buildProfilePayload(values),
      revision: editing.revision,
    });
  }
  return createOrchestration({
    agentCode: values.agentCode,
    ...buildProfilePayload(values),
  });
}

/** 把服务端返回的配置写回列表：更新走替换，新增走追加。 */
function mergeProfile(
  previous: AgentOrchestrationProfileRead[],
  saved: AgentOrchestrationProfileRead,
  isUpdate: boolean,
): AgentOrchestrationProfileRead[] {
  if (isUpdate) {
    return previous.map((profile) =>
      profile.agentCode === saved.agentCode ? saved : profile,
    );
  }
  return [
    ...previous.filter((profile) => profile.agentCode !== saved.agentCode),
    saved,
  ];
}

/** 提交表单：校验 → 创建/更新 → 回写列表；失败时按状态码给出提示。 */
async function submitProfile(options: ProfileSubmitOptions): Promise<void> {
  const { t, message, form, editing, setProfiles } = options;
  const { setSaveError, setModalOpen, setSaving, refreshProfiles } = options;
  const values = await readValidatedValues(form);
  if (!values) return;
  setSaving(true);
  setSaveError(null);
  try {
    const saved = await saveProfile(editing, values);
    setProfiles((previous) => mergeProfile(previous, saved, editing !== null));
    message.success(
      t(
        editing
          ? "agentOrchestration.messages.updated"
          : "agentOrchestration.messages.created",
      ),
    );
    setModalOpen(false);
  } catch (error: unknown) {
    setSaveError(
      editing
        ? describeFailure(t, error, "agentOrchestration.messages.updateFailed")
        : describeCreateFailure(t, error),
    );
    // 乐观锁冲突：拉一次最新列表，把服务端 revision 同步回来，用户可直接重试。
    if (errorStatus(error) === 409) void refreshProfiles();
  } finally {
    setSaving(false);
  }
}

/** 弹窗的打开 / 编辑 / 提交。 */
export function useProfileModal(options: ProfileModalOptions): ProfileModalState {
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  const openCreate = (): void => {
    options.setEditing(null);
    setSaveError(null);
    options.form.resetFields();
    options.form.setFieldsValue({ ...DEFAULT_PROFILE_VALUES, toolNames: [] });
    setModalOpen(true);
  };

  const openEdit = (profile: AgentOrchestrationProfileRead): void => {
    options.setEditing(profile);
    setSaveError(null);
    fillEditForm(options.form, profile);
    setModalOpen(true);
  };

  const closeModal = (): void => setModalOpen(false);

  const handleSubmit = (): Promise<void> =>
    submitProfile({
      t: options.t,
      message: options.message,
      form: options.form,
      editing: options.editing,
      setProfiles: options.setProfiles,
      setSaveError,
      setModalOpen,
      setSaving,
      refreshProfiles: options.refreshProfiles,
    });

  return {
    saving,
    saveError,
    modalOpen,
    openCreate,
    openEdit,
    closeModal,
    handleSubmit,
  };
}

/** 切换启用状态需要的上下文。 */
export interface ProfileToggleOptions {
  t: Translate;
  message: OrchestrationMessage;
  setProfiles: Dispatch<SetStateAction<AgentOrchestrationProfileRead[]>>;
  setPageError: Dispatch<SetStateAction<string | null>>;
  refreshProfiles: () => Promise<void>;
}

/** 切换配置启用状态。 */
export function useProfileToggle(options: ProfileToggleOptions): {
  handleToggle: (profile: AgentOrchestrationProfileRead) => Promise<void>;
} {
  const handleToggle = async (
    profile: AgentOrchestrationProfileRead,
  ): Promise<void> => {
    options.setPageError(null);
    try {
      const updated = await toggleOrchestration(profile.agentCode, {
        enabled: !profile.enabled,
        revision: profile.revision,
      });
      options.setProfiles((previous) =>
        previous.map((item) =>
          item.agentCode === updated.agentCode ? updated : item,
        ),
      );
      options.message.success(options.t("agentOrchestration.messages.toggled"));
    } catch (error: unknown) {
      options.setPageError(
        describeFailure(
          options.t,
          error,
          "agentOrchestration.messages.toggleFailed",
        ),
      );
      // 乐观锁冲突：拉一次最新列表，把服务端 revision 同步回来，用户可直接重试。
      if (errorStatus(error) === 409) void options.refreshProfiles();
    }
  };
  return { handleToggle };
}

/** 编排配置页的全部状态与动作。 */
export interface OrchestrationProfilesController {
  form: FormInstance<ProfileFormValues>;
  agentChoices: SelectChoice[];
  toolChoices: SelectChoice[];
  profiles: AgentOrchestrationProfileRead[];
  loading: boolean;
  saving: boolean;
  pageError: string | null;
  saveError: string | null;
  modalOpen: boolean;
  editing: AgentOrchestrationProfileRead | null;
  openCreate: () => void;
  openEdit: (profile: AgentOrchestrationProfileRead) => void;
  closeModal: () => void;
  refreshProfiles: () => Promise<void>;
  handleSubmit: () => Promise<void>;
  handleToggle: (profile: AgentOrchestrationProfileRead) => Promise<void>;
}

/** 编排配置页的状态控制器：数据 + 弹窗 + 切换。 */
export function useOrchestrationProfiles(
  t: Translate,
  message: OrchestrationMessage,
): OrchestrationProfilesController {
  const [form] = Form.useForm<ProfileFormValues>();
  const data = useOrchestrationData(t);
  const modal = useProfileModal({
    t,
    message,
    form,
    editing: data.editing,
    setEditing: data.setEditing,
    setProfiles: data.setProfiles,
    refreshProfiles: data.refreshProfiles,
  });
  const toggle = useProfileToggle({
    t,
    message,
    setProfiles: data.setProfiles,
    setPageError: data.setPageError,
    refreshProfiles: data.refreshProfiles,
  });
  return {
    form,
    agentChoices: data.agentChoices,
    toolChoices: data.toolChoices,
    profiles: data.profiles,
    loading: data.loading,
    saving: modal.saving,
    pageError: data.pageError,
    saveError: modal.saveError,
    modalOpen: modal.modalOpen,
    editing: data.editing,
    openCreate: modal.openCreate,
    openEdit: modal.openEdit,
    closeModal: modal.closeModal,
    refreshProfiles: data.refreshProfiles,
    handleSubmit: modal.handleSubmit,
    handleToggle: toggle.handleToggle,
  };
}
