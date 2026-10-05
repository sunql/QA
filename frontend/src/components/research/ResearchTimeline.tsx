/** 研究会话时间线（feat-research-entry Task 10）。
 *
 * 渲染 turns 序列（user / agent / checkpoint_awaiting 三类角色）+ 未被任何
 * checkpoint_awaiting 轮引用的「孤儿检查点」追加到末尾。每项带锚点
 * turn-<id> / checkpoint-<id>，点跳转按钮回调 onJump(anchor)。
 *
 * 用 antd Timeline 的 `items` API（children 模式要求直接子元素是 Timeline.Item，
 * 自定义包装组件会破坏 antd 的 cloneElement 注入）。
 */
import { Button, Tag, Timeline } from "antd";
import { useTranslation } from "react-i18next";
import type {
  CheckpointStatus,
  ResearchCheckpoint,
  ResearchTurn,
  TurnRole,
} from "../../types/research";

interface ResearchTimelineProps {
  turns: ResearchTurn[];
  checkpoints: ResearchCheckpoint[];
  onJump: (anchor: string) => void;
}

const STATUS_COLORS: Record<CheckpointStatus, string> = {
  pending: "processing",
  confirmed: "success",
  modified: "warning",
  rejected: "error",
};

function checkpointIdOf(turn: ResearchTurn): string {
  return typeof turn.content.checkpointId === "string" ? turn.content.checkpointId : "";
}

// 角色 → i18n key（缺 key 时 t 回退 key 自身，不炸）。
function roleKey(role: TurnRole): string {
  switch (role) {
    case "user":
      return "research.timeline.role.user";
    case "agent":
      return "research.timeline.role.agent";
    default:
      return "research.timeline.role.checkpoint";
  }
}

function statusKey(status: CheckpointStatus): string {
  return `research.timeline.status.${status}`;
}

function checkpointById(checkpoints: ResearchCheckpoint[]): Map<string, ResearchCheckpoint> {
  const map = new Map<string, ResearchCheckpoint>();
  for (const checkpoint of checkpoints) map.set(checkpoint.id, checkpoint);
  return map;
}

function referencedCheckpointIds(turns: ResearchTurn[]): Set<string> {
  const ids = new Set<string>();
  for (const turn of turns) {
    if (turn.role === "checkpoint_awaiting") {
      const id = checkpointIdOf(turn);
      if (id) ids.add(id);
    }
  }
  return ids;
}

// 轮次正文：user 取 question、agent 取 text/summary、checkpoint 取 prompt。
function turnText(turn: ResearchTurn): string {
  const { content } = turn;
  if (typeof content.question === "string") return content.question;
  if (typeof content.text === "string") return content.text;
  if (typeof content.summary === "string") return content.summary;
  if (typeof content.prompt === "string") return content.prompt;
  return "";
}

interface TurnContentProps {
  turn: ResearchTurn;
  checkpoint?: ResearchCheckpoint;
  onJump: (anchor: string) => void;
}

function TurnContent({ turn, checkpoint, onJump }: TurnContentProps) {
  const { t } = useTranslation();
  const anchor = `turn-${turn.id}`;
  const text = turnText(turn);
  return (
    <div id={anchor}>
      <Tag>{t(roleKey(turn.role))}</Tag>
      {checkpoint ? <Tag color={STATUS_COLORS[checkpoint.status]}>{t(statusKey(checkpoint.status))}</Tag> : null}
      <Button
        type="link"
        size="small"
        data-testid={`jump-${anchor}`}
        onClick={() => onJump(anchor)}
      >
        {t("research.timeline.jump")}
      </Button>
      {text ? <p>{text}</p> : null}
    </div>
  );
}

interface CheckpointContentProps {
  checkpoint: ResearchCheckpoint;
  onJump: (anchor: string) => void;
}

function CheckpointContent({ checkpoint, onJump }: CheckpointContentProps) {
  const { t } = useTranslation();
  const anchor = `checkpoint-${checkpoint.id}`;
  return (
    <div id={anchor}>
      <Tag>{t(roleKey("checkpoint_awaiting"))}</Tag>
      <Tag color={STATUS_COLORS[checkpoint.status]}>{t(statusKey(checkpoint.status))}</Tag>
      <Button
        type="link"
        size="small"
        data-testid={`jump-${anchor}`}
        onClick={() => onJump(anchor)}
      >
        {t("research.timeline.jump")}
      </Button>
      {checkpoint.prompt ? <p>{checkpoint.prompt}</p> : null}
    </div>
  );
}

export function ResearchTimeline({ turns, checkpoints, onJump }: ResearchTimelineProps) {
  const byId = checkpointById(checkpoints);
  const referencedIds = referencedCheckpointIds(turns);
  const orphans = checkpoints.filter((checkpoint) => !referencedIds.has(checkpoint.id));
  const items = [
    ...turns.map((turn) => ({
      key: turn.id,
      children: (
        <TurnContent
          turn={turn}
          checkpoint={turn.role === "checkpoint_awaiting" ? byId.get(checkpointIdOf(turn)) : undefined}
          onJump={onJump}
        />
      ),
    })),
    ...orphans.map((checkpoint) => ({
      key: checkpoint.id,
      children: <CheckpointContent checkpoint={checkpoint} onJump={onJump} />,
    })),
  ];
  return <Timeline items={items} />;
}
