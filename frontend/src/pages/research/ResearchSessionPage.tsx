/** 研究会话页（feat-research-entry Task 10，修复轮）。
 *
 * 流编排已抽到 useResearchSession：串行 openSession → connectStream（等 onOpen）→
 * submitTurn，避免「先提交后建流」竞态与 unhandled rejection。流未结束期间由
 * ResearchProgress 渲染 store.events 派生的实时进度；流结束后重拉历史补全时间线。
 */
import { useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { Alert, App, Button, Input, Space, Spin, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import { useResearchStore } from "../../stores/researchStore";
import { useResearchSession } from "../../hooks/useResearchSession";
import { useDatasourceOptions, datasourceName } from "../../hooks/useDatasourceOptions";
import { CheckpointCard } from "../../components/research/CheckpointCard";
import { ResearchTimeline } from "../../components/research/ResearchTimeline";
import { ResearchProgress } from "../../components/research/ResearchProgress";
import { ResearchDetailDrawer, type ResearchDetailPayload } from "../../components/research/ResearchDetailDrawer";
import type { CheckpointAction } from "../../types/research";

const { TextArea } = Input;

export default function ResearchSessionPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { t } = useTranslation();
  const { message, notification } = App.useApp();

  const turns = useResearchStore((s) => s.turns);
  const events = useResearchStore((s) => s.events);
  const pendingCheckpoint = useResearchStore((s) => s.pendingCheckpoint);
  const currentSession = useResearchStore((s) => s.currentSession);
  const streaming = useResearchStore((s) => s.streaming);
  const resolutionInFlight = useResearchStore((s) => s.resolutionInFlight);
  const error = useResearchStore((s) => s.error);
  const conflictError = useResearchStore((s) => s.conflictError);
  const submitTurn = useResearchStore((s) => s.submitTurn);
  const answer = useResearchStore((s) => s.answer);
  const clearConflictError = useResearchStore((s) => s.clearConflictError);
  const sources = useDatasourceOptions();

  const pendingQuestion =
    (location.state as { question?: string } | null)?.question ?? "";
  useResearchSession(id, pendingQuestion);

  const [input, setInput] = useState("");
  const [detailPayload, setDetailPayload] = useState<ResearchDetailPayload | null>(null);

  // B3.5: conflictError toast — dispatch clearConflictError after toast so it fires once.
  useEffect(() => {
    if (!conflictError) return;
    message.error(t(conflictError));
    clearConflictError();
  }, [conflictError, message, t, clearConflictError]);

  // 分析完成提示：events 数组追加 `research.done` 时弹一次 antd notification，
  // 提示用户去查看报告。改用 events 而不是 streaming 边沿：直接看后端真的发了
  // done 事件，绕开 streaming 状态机的边沿/批处理时序问题。
  // 用 lastSeenDoneEventsLen（events.length 快照）代替 ref 跨测试污染隐患。
  // conflictError 出现时重置快照使下次 done 仍可弹（不重置 == 旧 snapshot 挡）。
  const [lastSeenDoneEventsLen, setLastSeenDoneEventsLen] = useState<number>(-1);

  useEffect(() => {
    const last = events[events.length - 1];
    if (!last || last.name !== "research.done") return;
    if (lastSeenDoneEventsLen === events.length) return;
    // 来过 conflictError / 还在 resolutionInFlight：不再弹。snapshot 提前
    // 推进避免后续 SSE 真的发 done 时绕过这道闸（real path：conflict → 流关
    // → toast 消费 conflictError（自动 clear）→ 即使再 append done 也不该弹）。
    if (conflictError || resolutionInFlight) {
      setLastSeenDoneEventsLen(events.length);
      return;
    }
    setLastSeenDoneEventsLen(events.length);
    notification.success({
      message: t("research.session.complete_with_report"),
      description: t("research.checkpoint.processing"),
      actions: (
        <Button
          type="primary"
          size="small"
          onClick={() => id && navigate(`/research/${id}/report`)}
        >
          {t("research.session.view_report")}
        </Button>
      ),
      duration: 6,
      placement: "topRight",
    });
  }, [events, resolutionInFlight, conflictError, lastSeenDoneEventsLen, id, navigate, t]);

  const handleSend = async () => {
    const trimmed = input.trim();
    if (!trimmed || !id) return;
    setInput("");
    try {
      await submitTurn(id, trimmed);
    } catch {
      message.error(t("research.session.error.default"));
    }
  };

  const handleAnswer = async (action: CheckpointAction, choice: Record<string, unknown>) => {
    if (!pendingCheckpoint) return;
    try {
      await answer(pendingCheckpoint.id, action, choice);
    } catch {
      message.error(t("research.session.error.default"));
    }
  };

  const handleDetail = (payload: ResearchDetailPayload) => {
    setDetailPayload(payload);
  };
  const closeDetail = () => setDetailPayload(null);

  return (
    <div style={{ padding: 16 }}>
      <Space style={{ marginBottom: 16 }}>
        <Button onClick={() => navigate("/research")}>{t("research.session.back")}</Button>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {currentSession?.title || t("research.session.title")}
        </Typography.Title>
        {datasourceName(sources, currentSession?.datasourceId) ? (
          <Tag>{datasourceName(sources, currentSession?.datasourceId)}</Tag>
        ) : null}
        <Button
          type="primary"
          onClick={() => id && navigate(`/research/${id}/report`)}
        >
          {t("research.session.report")}
        </Button>
      </Space>
      {error ? <Alert type="error" message={error} style={{ marginBottom: 16 }} /> : null}
      {streaming ? <ResearchProgress events={events} /> : null}
      <Spin spinning={streaming && turns.length === 0}>
        <ResearchTimeline
          turns={turns}
          checkpoints={pendingCheckpoint ? [pendingCheckpoint] : []}
          onDetail={handleDetail}
        />
      </Spin>
      <ResearchDetailDrawer payload={detailPayload} onClose={closeDetail} />
      {pendingCheckpoint ? (
        <CheckpointCard
          key={pendingCheckpoint.id}
          checkpoint={pendingCheckpoint}
          onAnswer={handleAnswer}
          resolutionInFlight={resolutionInFlight}
        />
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
