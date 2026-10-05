/** 研究入口 — 会话列表 + 发起新研究（feat-research-entry Task 10）。
 *
 * 新建会话只做「创建 + 跳转」：不在此处提交首轮 turn（首轮必须由会话页
 * 先 connectStream 再 submitTurn，否则后端不重放历史、进度会被漏掉 —— 见
 * researchStore 的注释）。首轮问题经 location.state 带给会话页。
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { App, Button, Card, Checkbox, Empty, Input, List, Popconfirm, Select, Space, Tag, Typography } from "antd";
import { DeleteOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { useResearchStore } from "../../stores/researchStore";
import { useDatasourceOptions, datasourceName } from "../../hooks/useDatasourceOptions";
import { createResearchSession } from "../../api/research";
import type { ResearchMode } from "../../types/research";

const { TextArea } = Input;

const MODE_OPTIONS: ReadonlyArray<{ value: ResearchMode; labelKey: string; descKey: string }> = [
  { value: "research", labelKey: "research.list.mode.research", descKey: "research.list.modeDesc.research" },
  { value: "attribution", labelKey: "research.list.mode.attribution", descKey: "research.list.modeDesc.attribution" },
  { value: "compare", labelKey: "research.list.mode.compare", descKey: "research.list.modeDesc.compare" },
];

/** 模式下拉的两行选项：主标题 + 副描述（副描述说明该模式只改变报告的章节组织）。 */
function ModeOptionLabel({ title, desc }: { title: string; desc: string }) {
  return (
    <div>
      <div>{title}</div>
      <div style={{ fontSize: 12, color: "#8c8c8c" }}>{desc}</div>
    </div>
  );
}

export default function ResearchListPage() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const sessions = useResearchStore((s) => s.sessions);
  const sessionsLoading = useResearchStore((s) => s.sessionsLoading);
  const loadSessions = useResearchStore((s) => s.loadSessions);
  const deleteSession = useResearchStore((s) => s.deleteSession);
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<ResearchMode>("research");
  const [submitting, setSubmitting] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const sources = useDatasourceOptions();
  const [datasourceId, setDatasourceId] = useState<number | null>(null);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    if (datasourceId !== null) return;
    const preferred = sources.find((source) => source.isDefault) ?? sources[0];
    if (preferred) setDatasourceId(preferred.id);
  }, [sources, datasourceId]);

  const startResearch = async () => {
    const trimmed = question.trim();
    if (trimmed.length === 0) return;
    setSubmitting(true);
    try {
      const session = await createResearchSession({
        question: trimmed,
        mode,
        datasourceId,
      });
      navigate(`/research/${session.id}`, { state: { question: trimmed } });
    } catch {
      message.error(t("research.session.error"));
    } finally {
      setSubmitting(false);
    }
  };

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id],
    );
  };

  const handleDelete = async (sessionId: string) => {
    try {
      await deleteSession(sessionId);
      // 已删会话不能留在多选集合里，否则「对比」会带上一个不存在的 id。
      setSelectedIds((prev) => prev.filter((item) => item !== sessionId));
    } catch {
      message.error(t("research.list.deleteError"));
    }
  };

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
      <List
        loading={sessionsLoading}
        dataSource={sessions}
        locale={{ emptyText: <Empty description={t("research.list.empty")} /> }}
        renderItem={(session) => {
          const selected = selectedIds.includes(session.id);
          // 行标识：与行内可见标题同源（title 为空时回落 question），
          // 既作复选框标签，也拼进删除按钮的可访问名 —— 多行列表里删除按钮必须
          // 逐行可辨（扁平 aria-label 会让每一行同名，读屏用户无法区分删的是哪条）。
          const rowLabel = session.title || session.question;
          return (
            <List.Item
              actions={[
                <Button key="open" type="link" onClick={() => navigate(`/research/${session.id}`)}>
                  {t("research.list.open")}
                </Button>,
                <Button
                  key="report"
                  type="link"
                  onClick={() => navigate(`/research/${session.id}/report`)}
                >
                  {t("research.list.report")}
                </Button>,
                <Popconfirm
                  key="delete"
                  title={t("research.list.deleteConfirm")}
                  okText={t("common.confirm")}
                  cancelText={t("common.cancel")}
                  onConfirm={() => handleDelete(session.id)}
                >
                  <Button
                    type="link"
                    danger
                    icon={<DeleteOutlined />}
                    aria-label={`${t("research.list.deleteAriaLabel")}：${rowLabel}`}
                  >
                    {t("common.delete")}
                  </Button>
                </Popconfirm>,
              ]}
            >
              <Checkbox
                checked={selected}
                onChange={() => toggleSelect(session.id)}
                aria-label={rowLabel}
              />
              <List.Item.Meta title={rowLabel} description={session.question} />
              {datasourceName(sources, session.datasourceId) ? (
                <Tag>{datasourceName(sources, session.datasourceId)}</Tag>
              ) : null}
              <Tag>{t(`research.list.mode.${session.mode}`)}</Tag>
            </List.Item>
          );
        }}
      />
    </div>
  );
}
