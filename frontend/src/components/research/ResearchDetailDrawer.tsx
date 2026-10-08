/** 研究会话时间线「查看」按钮触发的详情 Drawer（2026-10-08 替换定位按钮）。
 *
 * 右侧 Drawer 展示一轮的完整结构化内容：user 轮原样展示 question；checkpoint
 * 轮按 phase 形态结构化渲染 prompt + options（hypothesis 展示候选列表、planning
 * 展示步骤、runtime_dynamic 展示指标/冲突、low_confidence_step 展示低置信度步骤）。
 * 数据源**用 ResearchCheckpoint 当前态**（不是 turn.content 的快照）：检查点
 * 决策后 userChoice 写在 checkpoint 行上，turn.content 是开检查点那一刻的快照。
 */
import { Descriptions, Drawer, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";
import type { ResearchCheckpoint, ResearchTurn } from "../../types/research";

const { Paragraph } = Typography;

// 与 ResearchTimeline 的 ResearchDetailPayload 同源（共享判别联合）。本文件
// 单独重写是因为不想在 Drawer 内部跨组件 import 一个 DOM 组件的内部类型（耦合）。
export type ResearchDetailPayload =
  | { kind: "user"; turn: ResearchTurn }
  | { kind: "checkpoint"; turn: ResearchTurn; checkpoint: ResearchCheckpoint }
  | { kind: "orphan"; checkpoint: ResearchCheckpoint };

const PHASE_COLORS: Record<string, string> = {
  intent: "blue",
  planning: "geekblue",
  hypothesis: "purple",
  runtime_dynamic: "cyan",
  low_confidence_step: "orange",
};

const STATUS_COLORS: Record<string, string> = {
  pending: "processing",
  confirmed: "success",
  modified: "warning",
  rejected: "error",
};

interface ResearchDetailDrawerProps {
  payload: ResearchDetailPayload | null;
  onClose: () => void;
}

interface CandidateOption {
  statement?: string;
  driver?: string;
  text?: string;
  id?: string;
  title?: string;
  reason?: string;
}

function asArray(value: unknown): CandidateOption[] {
  return Array.isArray(value) ? (value as CandidateOption[]) : [];
}

// 按 phase 形态把 options 拆成可读列表。未知 phase 退化为 JSON 展示，保证不丢字段。
function renderOptionsByPhase(phase: string, options: Record<string, unknown>) {
  switch (phase) {
    case "hypothesis": {
      const candidates = asArray(options.candidates);
      if (candidates.length === 0) return null;
      return (
        <ul data-testid="detail-options-candidates">
          {candidates.map((c, idx) => (
            <li key={idx}>
              <strong>{c.statement ?? c.text ?? `候选 ${idx + 1}`}</strong>
              {c.driver ? <span style={{ marginLeft: 8, color: "#888" }}>driver: {c.driver}</span> : null}
            </li>
          ))}
        </ul>
      );
    }
    case "planning": {
      const steps = asArray(options.steps).length > 0
        ? asArray(options.steps)
        : asArray((options.plan as Record<string, unknown> | undefined)?.steps);
      if (steps.length === 0) return null;
      return (
        <ol data-testid="detail-options-steps">
          {steps.map((s, idx) => (
            <li key={s.id ?? idx}>
              <strong>{s.title ?? s.text ?? `步骤 ${idx + 1}`}</strong>
              {s.reason ? <div style={{ color: "#666" }}>{s.reason}</div> : null}
            </li>
          ))}
        </ol>
      );
    }
    case "runtime_dynamic": {
      const arms = (options.arms as Record<string, unknown> | undefined) ?? {};
      const metrics = asArray(arms.metrics);
      const conflicts = asArray(arms.conflicts);
      if (metrics.length === 0 && conflicts.length === 0) return null;
      return (
        <div data-testid="detail-options-arms">
          {metrics.length > 0 ? (
            <div>
              <strong>指标</strong>
              <ul>{metrics.map((m, idx) => <li key={idx}>{m.text ?? m.statement ?? `指标 ${idx + 1}`}</li>)}</ul>
            </div>
          ) : null}
          {conflicts.length > 0 ? (
            <div>
              <strong>知识冲突</strong>
              <ul>{conflicts.map((c, idx) => <li key={idx}>{c.text ?? c.statement ?? `冲突 ${idx + 1}`}</li>)}</ul>
            </div>
          ) : null}
        </div>
      );
    }
    case "low_confidence_step": {
      const lowSteps = asArray(options.lowSteps).length > 0
        ? asArray(options.lowSteps)
        : asArray(options.context);
      if (lowSteps.length === 0) return null;
      return (
        <ul data-testid="detail-options-low-steps">
          {lowSteps.map((s, idx) => (
            <li key={idx}>
              {typeof s === "string" ? s : s.text ?? s.title ?? JSON.stringify(s)}
            </li>
          ))}
        </ul>
      );
    }
    default:
      return (
        <pre data-testid="detail-options-json" style={{ background: "#f5f5f5", padding: 8, borderRadius: 4 }}>
          {JSON.stringify(options, null, 2)}
        </pre>
      );
  }
}

function CheckpointDetail({
  checkpoint,
  turnCreatedAt,
}: {
  checkpoint: ResearchCheckpoint;
  turnCreatedAt?: string;
}) {
  const options = (checkpoint.options ?? {}) as Record<string, unknown>;
  return (
    <>
      <Descriptions column={1} size="small" style={{ marginBottom: 12 }}>
        <Descriptions.Item label="phase">
          <Tag color={PHASE_COLORS[checkpoint.phase] ?? "default"}>{checkpoint.phase}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="status">
          <Tag color={STATUS_COLORS[checkpoint.status] ?? "default"}>{checkpoint.status}</Tag>
        </Descriptions.Item>
        {turnCreatedAt ? <Descriptions.Item label="openedAt">{turnCreatedAt}</Descriptions.Item> : null}
        {checkpoint.decidedAt ? (
          <Descriptions.Item label="decidedAt">{checkpoint.decidedAt}</Descriptions.Item>
        ) : null}
      </Descriptions>
      {checkpoint.prompt ? (
        <>
          <Typography.Title level={5} style={{ marginTop: 0 }}>prompt</Typography.Title>
          <Paragraph style={{ whiteSpace: "pre-wrap" }}>{checkpoint.prompt}</Paragraph>
        </>
      ) : null}
      <Typography.Title level={5}>options</Typography.Title>
      {renderOptionsByPhase(checkpoint.phase, options)}
      {checkpoint.userChoice ? (
        <>
          <Typography.Title level={5}>你的选择</Typography.Title>
          <pre style={{ background: "#f0f5ff", padding: 8, borderRadius: 4, whiteSpace: "pre-wrap" }}>
            {JSON.stringify(checkpoint.userChoice, null, 2)}
          </pre>
        </>
      ) : null}
    </>
  );
}

export function ResearchDetailDrawer({ payload, onClose }: ResearchDetailDrawerProps) {
  const { t } = useTranslation();
  const open = payload !== null;
  // title 用 payload kind 区分；key 强制重挂载保证不同 payload 不复用同一 body。
  const body = (() => {
    if (!payload) return null;
    if (payload.kind === "user") {
      const question = typeof payload.turn.content.question === "string" ? payload.turn.content.question : "";
      return (
        <>
          <Descriptions column={1} size="small" style={{ marginBottom: 12 }}>
            <Descriptions.Item label="role">user</Descriptions.Item>
            <Descriptions.Item label="turnIndex">{payload.turn.turnIndex}</Descriptions.Item>
            <Descriptions.Item label="createdAt">{payload.turn.createdAt}</Descriptions.Item>
          </Descriptions>
          <Typography.Title level={5} style={{ marginTop: 0 }}>question</Typography.Title>
          <Paragraph style={{ whiteSpace: "pre-wrap" }}>{question}</Paragraph>
        </>
      );
    }
    if (payload.kind === "checkpoint") {
      return <CheckpointDetail checkpoint={payload.checkpoint} turnCreatedAt={payload.turn.createdAt} />;
    }
    return <CheckpointDetail checkpoint={payload.checkpoint} />;
  })();

  return (
    <Drawer
      title={t("research.timeline.detailTitle")}
      open={open}
      onClose={onClose}
      width={520}
      destroyOnClose
      data-testid="research-detail-drawer"
    >
      {body}
    </Drawer>
  );
}
