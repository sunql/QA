/** 研究会话页（feat-research-entry Task 10）。
 *
 * 流编排：挂载时 openSession（拉历史轮次）+ connectStream（订阅进度），若从列表页
 * 带首轮问题过来（location.state.question）则 submitTurn 提交首轮（先建流后提交，
 * 后端不重放历史）。checkpoint 经 CheckpointCard 决策；流结束后重拉一轮历史补全时间线。
 */
import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { Alert, App, Button, Input, Space, Spin, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { useResearchStore } from "../../stores/researchStore";
import { CheckpointCard } from "../../components/research/CheckpointCard";
import { ResearchTimeline } from "../../components/research/ResearchTimeline";
import type { CheckpointAction } from "../../types/research";

const { TextArea } = Input;

export default function ResearchSessionPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { t } = useTranslation();
  const { message } = App.useApp();

  const turns = useResearchStore((s) => s.turns);
  const pendingCheckpoint = useResearchStore((s) => s.pendingCheckpoint);
  const currentSession = useResearchStore((s) => s.currentSession);
  const streaming = useResearchStore((s) => s.streaming);
  const error = useResearchStore((s) => s.error);
  const openSession = useResearchStore((s) => s.openSession);
  const connectStream = useResearchStore((s) => s.connectStream);
  const submitTurn = useResearchStore((s) => s.submitTurn);
  const answer = useResearchStore((s) => s.answer);

  const [input, setInput] = useState("");
  const firstTurnSent = useRef(false);
  const wasStreaming = useRef(false);

  const pendingQuestion =
    (location.state as { question?: string } | null)?.question ?? "";

  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    void openSession(id);
    void connectStream(id, controller.signal);
    if (pendingQuestion && !firstTurnSent.current) {
      firstTurnSent.current = true;
      void submitTurn(id, pendingQuestion);
    }
    return () => controller.abort();
  }, [id, pendingQuestion, openSession, connectStream, submitTurn]);

  // 流结束（done / 终态 error）后重拉一轮历史，把完整 turn 时间线补进 store。
  useEffect(() => {
    if (!id) return;
    if (wasStreaming.current && !streaming) {
      void openSession(id);
    }
    wasStreaming.current = streaming;
  }, [id, streaming, openSession]);

  const handleSend = async () => {
    const trimmed = input.trim();
    if (!trimmed || !id) return;
    setInput("");
    try {
      await submitTurn(id, trimmed);
    } catch {
      message.error(t("research.session.error"));
    }
  };

  const handleAnswer = async (action: CheckpointAction, choice: Record<string, unknown>) => {
    if (!pendingCheckpoint) return;
    try {
      await answer(pendingCheckpoint.id, action, choice);
    } catch {
      message.error(t("research.session.error"));
    }
  };

  const handleJump = (anchor: string) => {
    document.getElementById(anchor)?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <div style={{ padding: 16 }}>
      <Space style={{ marginBottom: 16 }}>
        <Button onClick={() => navigate("/research")}>{t("research.session.back")}</Button>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {currentSession?.title || t("research.session.title")}
        </Typography.Title>
        <Button
          type="primary"
          onClick={() => id && navigate(`/research/${id}/report`)}
        >
          {t("research.session.report")}
        </Button>
      </Space>
      {error ? <Alert type="error" message={error} style={{ marginBottom: 16 }} /> : null}
      <Spin spinning={streaming && turns.length === 0}>
        <ResearchTimeline
          turns={turns}
          checkpoints={pendingCheckpoint ? [pendingCheckpoint] : []}
          onJump={handleJump}
        />
      </Spin>
      {pendingCheckpoint ? (
        <CheckpointCard checkpoint={pendingCheckpoint} onAnswer={handleAnswer} />
      ) : (
        <Space.Compact style={{ width: "100%", marginTop: 16 }}>
          <TextArea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder={t("research.session.inputPlaceholder")}
            autoSize={{ minRows: 2 }}
          />
          <Button type="primary" onClick={handleSend}>
            {t("research.session.send")}
          </Button>
        </Space.Compact>
      )}
    </div>
  );
}
