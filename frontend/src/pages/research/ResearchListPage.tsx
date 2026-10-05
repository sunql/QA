/** 研究入口 — 会话列表 + 发起新研究（feat-research-entry Task 10）。
 *
 * 新建会话只做「创建 + 跳转」：不在此处提交首轮 turn（首轮必须由会话页
 * 先 connectStream 再 submitTurn，否则后端不重放历史、进度会被漏掉 —— 见
 * researchStore 的注释）。首轮问题经 location.state 带给会话页。
 *
 * 结构（feat-research-entry-ux-fixes）：主组件只做「拉列表 + 组合」，表单 / 列表 /
 * 行 / 行内删除动作各自是本文件的模块级子组件。模块级是硬约束 —— 组件若定义在父
 * 函数内部，每次父渲染都会重挂载，表单输入与勾选态会当场丢失。
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { App, Button, Card, Checkbox, Empty, Input, List, Popconfirm, Select, Space, Tag, Typography } from "antd";
import { DeleteOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { useResearchStore } from "../../stores/researchStore";
import { useDatasourceOptions, datasourceName } from "../../hooks/useDatasourceOptions";
import { createResearchSession } from "../../api/research";
import { listModels } from "../../api/modelConfig";
import type { DataSource } from "../../types/datasource";
import type { ModelConfig } from "../../types/modelConfig";
import type { ResearchMode, ResearchSession } from "../../types/research";

const { TextArea } = Input;

const MODE_OPTIONS: ReadonlyArray<{ value: ResearchMode; labelKey: string; descKey: string }> = [
  { value: "research", labelKey: "research.list.mode.research", descKey: "research.list.modeDesc.research" },
  { value: "attribution", labelKey: "research.list.mode.attribution", descKey: "research.list.modeDesc.attribution" },
  { value: "compare", labelKey: "research.list.mode.compare", descKey: "research.list.modeDesc.compare" },
];

interface ModeOptionLabelProps {
  title: string;
  desc: string;
}

/** 模式下拉的两行选项：主标题 + 副描述（副描述说明该模式只改变报告的章节组织）。 */
function ModeOptionLabel({ title, desc }: ModeOptionLabelProps) {
  return (
    <div>
      <div>{title}</div>
      <div style={{ fontSize: 12, color: "#8c8c8c" }}>{desc}</div>
    </div>
  );
}

interface NewResearchForm {
  question: string;
  mode: ResearchMode;
  datasourceId: number | null;
  models: ModelConfig[];
  modelId: number | null;
  submitting: boolean;
  setQuestion: (value: string) => void;
  setMode: (value: ResearchMode) => void;
  setDatasourceId: (value: number | null) => void;
  setModelId: (value: number | null) => void;
  startResearch: () => Promise<void>;
}

/** 新建表单的状态与提交：创建成功即携带首轮问题跳转到会话页（首轮不在此提交）。 */
function useNewResearchForm(sources: DataSource[]): NewResearchForm {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<ResearchMode>("research");
  const [submitting, setSubmitting] = useState(false);
  const [datasourceId, setDatasourceId] = useState<number | null>(null);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [modelId, setModelId] = useState<number | null>(null);

  // 默认选中默认数据源；用户已选过（或清单后到）都不再覆盖。
  useEffect(() => {
    if (datasourceId !== null) return;
    const preferred = sources.find((source) => source.isDefault) ?? sources[0];
    if (preferred) setDatasourceId(preferred.id);
  }, [sources, datasourceId]);

  useEffect(() => {
    void (async () => {
      try {
        const list = await listModels(true);
        if (Array.isArray(list)) setModels(list);
      } catch {
        // 展示层降级：清单缺失时下拉为空，用户仍可「自动」建会话。
      }
    })();
  }, []);

  const startResearch = async () => {
    const trimmed = question.trim();
    if (trimmed.length === 0) return;
    setSubmitting(true);
    try {
      // 条件展开：不选模型时不放 modelId 键，「自动」与今天逐字一致（后端 extra=forbid）。
      const session = await createResearchSession({
        question: trimmed,
        mode,
        datasourceId,
        ...(modelId === null ? {} : { modelId }),
      });
      navigate(`/research/${session.id}`, { state: { question: trimmed } });
    } catch {
      message.error(t("research.session.error"));
    } finally {
      setSubmitting(false);
    }
  };

  return {
    question,
    mode,
    datasourceId,
    models,
    modelId,
    submitting,
    setQuestion,
    setMode,
    setDatasourceId,
    setModelId,
    startResearch,
  };
}

interface NewResearchCardProps {
  sources: DataSource[];
}

/** 发起新研究卡片：问题 + 数据源 + 模型 + 模式；模式选项带副描述。 */
function NewResearchCard({ sources }: NewResearchCardProps) {
  const { t } = useTranslation();
  const {
    question,
    mode,
    datasourceId,
    models,
    modelId,
    submitting,
    setQuestion,
    setMode,
    setDatasourceId,
    setModelId,
    startResearch,
  } = useNewResearchForm(sources);

  return (
    <Card size="small" style={{ marginBottom: 16 }}>
      <Space direction="vertical" style={{ width: "100%" }}>
        <TextArea
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder={t("research.list.questionPlaceholder")}
          autoSize={{ minRows: 2 }}
        />
        <Space>
          <Select<number>
            value={datasourceId ?? undefined}
            onChange={setDatasourceId}
            placeholder={t("research.list.datasourcePlaceholder")}
            options={sources.map((source) => ({ value: source.id, label: source.name }))}
          />
          {/* 0 是「自动」哨兵项（真实模型 id 从 1 起）：value 为 undefined 时 antd 只显示
              占位符，用户看不出默认是自动。请求体仍按 modelId === null 判断是否带键。 */}
          <Select<number>
            value={modelId ?? 0}
            onChange={(value) => setModelId(value === 0 ? null : value)}
            placeholder={t("research.list.modelPlaceholder")}
            options={[
              { value: 0, label: t("research.list.modelAuto") },
              ...models.map((model) => ({ value: model.id, label: model.modelName })),
            ]}
          />
          <Select<ResearchMode>
            value={mode}
            onChange={setMode}
            options={MODE_OPTIONS.map((option) => ({
              value: option.value,
              label: <ModeOptionLabel title={t(option.labelKey)} desc={t(option.descKey)} />,
            }))}
          />
          <Button type="primary" loading={submitting} onClick={startResearch}>
            {t("research.list.start")}
          </Button>
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {t("research.list.modeHint")}
        </Typography.Text>
      </Space>
    </Card>
  );
}

interface SessionDeleteActionProps {
  sessionId: string;
  rowLabel: string;
  onDelete: (id: string) => void;
}

/** 行内删除动作（二次确认）。可访问名必须带行标识 —— 扁平 aria-label 会让每行同名，
 *  读屏用户无法区分删的是哪条；`rowLabel` 与行内可见标题同源（title 空则回落 question）。 */
function SessionDeleteAction({ sessionId, rowLabel, onDelete }: SessionDeleteActionProps) {
  const { t } = useTranslation();
  return (
    <Popconfirm
      title={t("research.list.deleteConfirm")}
      okText={t("common.confirm")}
      cancelText={t("common.cancel")}
      onConfirm={() => onDelete(sessionId)}
    >
      <Button
        type="link"
        danger
        icon={<DeleteOutlined />}
        aria-label={t("research.list.deleteAriaLabel", { name: rowLabel })}
      >
        {t("common.delete")}
      </Button>
    </Popconfirm>
  );
}

interface SessionRowProps {
  session: ResearchSession;
  sources: DataSource[];
  selected: boolean;
  onToggleSelect: (id: string) => void;
  onDelete: (id: string) => void;
}

/** 单条会话行：复选框 + 标题/问题 + 数据源 Tag + 模式 Tag + 三动作。 */
function SessionRow({ session, sources, selected, onToggleSelect, onDelete }: SessionRowProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  // 行标识与行内可见标题同源：既作复选框标签，也作删除按钮的可访问名。同时保证逐行唯一，
  // 扁平名会让每一行的删除按钮同名。
  const rowLabel = session.title || session.question;
  // 数据源名每行只算一次：守卫与 Tag 共用同一结果。
  const sourceLabel = datasourceName(sources, session.datasourceId);

  return (
    <List.Item
      actions={[
        <Button key="open" type="link" onClick={() => navigate(`/research/${session.id}`)}>
          {t("research.list.open")}
        </Button>,
        <Button key="report" type="link" onClick={() => navigate(`/research/${session.id}/report`)}>
          {t("research.list.report")}
        </Button>,
        <SessionDeleteAction
          key="delete"
          sessionId={session.id}
          rowLabel={rowLabel}
          onDelete={onDelete}
        />,
      ]}
    >
      <Checkbox checked={selected} onChange={() => onToggleSelect(session.id)} aria-label={rowLabel} />
      <List.Item.Meta title={rowLabel} description={session.question} />
      {sourceLabel ? <Tag>{sourceLabel}</Tag> : null}
      <Tag>{t(`research.list.mode.${session.mode}`)}</Tag>
    </List.Item>
  );
}

interface SessionListProps {
  sessions: ResearchSession[];
  loading: boolean;
  sources: DataSource[];
  selectedIds: string[];
  onToggleSelect: (id: string) => void;
  onDelete: (id: string) => void;
}

/** 会话列表容器（空态文案由 locale 提供）。 */
function SessionList({ sessions, loading, sources, selectedIds, onToggleSelect, onDelete }: SessionListProps) {
  const { t } = useTranslation();
  return (
    <List
      loading={loading}
      dataSource={sessions}
      locale={{ emptyText: <Empty description={t("research.list.empty")} /> }}
      renderItem={(session) => (
        <SessionRow
          session={session}
          sources={sources}
          selected={selectedIds.includes(session.id)}
          onToggleSelect={onToggleSelect}
          onDelete={onDelete}
        />
      )}
    />
  );
}

interface SessionSelection {
  selectedIds: string[];
  toggleSelect: (id: string) => void;
  removeSession: (id: string) => Promise<void>;
}

/** 多选与删除。删除成功后必须把该 id 移出多选集合，否则「对比」会带上一个不存在的会话。 */
function useSessionSelection(): SessionSelection {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const deleteSession = useResearchStore((s) => s.deleteSession);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id],
    );
  };

  const removeSession = async (id: string) => {
    try {
      await deleteSession(id);
      setSelectedIds((prev) => prev.filter((item) => item !== id));
    } catch {
      message.error(t("research.list.deleteError"));
    }
  };

  return { selectedIds, toggleSelect, removeSession };
}

export default function ResearchListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const sessions = useResearchStore((s) => s.sessions);
  const sessionsLoading = useResearchStore((s) => s.sessionsLoading);
  const loadSessions = useResearchStore((s) => s.loadSessions);
  const sources = useDatasourceOptions();
  const { selectedIds, toggleSelect, removeSession } = useSessionSelection();

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  const openCompare = () => {
    if (selectedIds.length < 2) return;
    navigate(`/research/compare?ids=${selectedIds.join(",")}`);
  };

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {t("research.list.title")}
        </Typography.Title>
        <Button type="primary" disabled={selectedIds.length < 2} onClick={openCompare}>
          {t("research.list.compare")}
        </Button>
      </div>
      <NewResearchCard sources={sources} />
      <SessionList
        sessions={sessions}
        loading={sessionsLoading}
        sources={sources}
        selectedIds={selectedIds}
        onToggleSelect={toggleSelect}
        onDelete={removeSession}
      />
    </div>
  );
}
