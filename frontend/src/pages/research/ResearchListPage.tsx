/** 研究入口 — 会话列表 + 发起新研究（feat-research-entry Task 10）。
 *
 * 新建会话只做「创建 + 跳转」：不在此处提交首轮 turn（首轮必须由会话页
 * 先 connectStream 再 submitTurn，否则后端不重放历史、进度会被漏掉 —— 见
 * researchStore 的注释）。首轮问题经 location.state 带给会话页。
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { App, Button, Card, Empty, Input, List, Select, Space, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { useResearchStore } from "../../stores/researchStore";
import { createResearchSession } from "../../api/research";
import type { ResearchMode } from "../../types/research";

const { TextArea } = Input;

const MODE_OPTIONS: ReadonlyArray<{ value: ResearchMode; labelKey: string }> = [
  { value: "research", labelKey: "research.list.mode.research" },
  { value: "attribution", labelKey: "research.list.mode.attribution" },
  { value: "compare", labelKey: "research.list.mode.compare" },
];

export default function ResearchListPage() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const sessions = useResearchStore((s) => s.sessions);
  const sessionsLoading = useResearchStore((s) => s.sessionsLoading);
  const loadSessions = useResearchStore((s) => s.loadSessions);
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<ResearchMode>("research");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  const startResearch = async () => {
    const trimmed = question.trim();
    if (trimmed.length === 0) return;
    setSubmitting(true);
    try {
      const session = await createResearchSession({ question: trimmed, mode });
      navigate(`/research/${session.id}`, { state: { question: trimmed } });
    } catch {
      message.error(t("research.session.error"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{ padding: 16 }}>
      <Typography.Title level={4}>{t("research.list.title")}</Typography.Title>
      <Card size="small" style={{ marginBottom: 16 }}>
        <Space direction="vertical" style={{ width: "100%" }}>
          <TextArea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder={t("research.list.questionPlaceholder")}
            autoSize={{ minRows: 2 }}
          />
          <Space>
            <Select<ResearchMode>
              value={mode}
              onChange={setMode}
              options={MODE_OPTIONS.map((option) => ({
                value: option.value,
                label: t(option.labelKey),
              }))}
            />
            <Button type="primary" loading={submitting} onClick={startResearch}>
              {t("research.list.start")}
            </Button>
          </Space>
        </Space>
      </Card>
      <List
        loading={sessionsLoading}
        dataSource={sessions}
        locale={{ emptyText: <Empty description={t("research.list.empty")} /> }}
        renderItem={(session) => (
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
            ]}
          >
            <List.Item.Meta title={session.title || session.question} description={session.question} />
            <Tag>{t(`research.list.mode.${session.mode}`)}</Tag>
          </List.Item>
        )}
      />
    </div>
  );
}
