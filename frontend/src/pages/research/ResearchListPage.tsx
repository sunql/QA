/** 研究入口 — 发起新研究。
 *
 * 历史会话迁到独立路由 `/research/history`（ResearchHistoryPage）：本页面只
 * 留「创建 + 跳转」，主内容只剩新建研究表单 + 顶部「查看历史」入口。旧版把
 * 列表塞到右侧 Drawer 反复遇到 useScrollLocker 在 jsdom 下同步阻塞 user.click
 * 的环境问题，且「对比」按钮需要先选 2+ 项才能启用，UX 上从 Drawer 里勾选
 * 也不直观——分两个页面更清晰。
 *
 * 新建会话只做「创建 + 跳转」：不在此处提交首轮 turn（首轮必须由会话页
 * 先 connectStream 再 submitTurn，否则后端不重放历史、进度会被漏掉 —— 见
 * researchStore 的注释）。首轮问题经 location.state 带给会话页。
 *
 * 结构（feat-research-entry-ux-fixes）：主组件只做「组合」，表单是本文件
 * 的模块级子组件。模块级是硬约束 —— 组件若定义在父函数内部，每次父渲染
 * 都会重挂载，表单输入会当场丢失。
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { App, Button, Card, Input, Select, Space, Typography } from "antd";
import { HistoryOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { useDatasourceOptions } from "../../hooks/useDatasourceOptions";
import { createResearchSession } from "../../api/research";
import { listModels } from "../../api/modelConfig";
import type { DataSource } from "../../types/datasource";
import type { ModelConfig } from "../../types/modelConfig";
import type { ResearchMode } from "../../types/research";

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
      message.error(t("research.session.error.default"));
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

export default function ResearchListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const sources = useDatasourceOptions();

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {t("research.list.title")}
        </Typography.Title>
        {/* 历史会话拆到独立路由 /research/history 维护（多选/删除/对比都在那边），
            本页只做「发起新研究」。点此按钮直达历史页。 */}
        <Button
          icon={<HistoryOutlined />}
          onClick={() => navigate("/research/history")}
        >
          {t("research.list.history")}
        </Button>
      </div>
      <NewResearchCard sources={sources} />
    </div>
  );
}
