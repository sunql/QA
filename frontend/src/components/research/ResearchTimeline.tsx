/** 研究会话时间线（feat-research-entry Task 10，详情按钮替换轮）。
 *
 * 渲染 turns 序列（user / agent / checkpoint_awaiting 三类角色）+ 未被任何
 * checkpoint_awaiting 轮引用的「孤儿检查点」追加到末尾。每项带「查看」按钮
 * 触发 onDetail(payload)，payload 是区分 kind 的结构体（user / checkpoint /
 * orphan 三种形态），由 ResearchSessionPage 决定如何展示（当前用右侧 Drawer）。
 *
 * 用 antd Timeline 的 `items` API（children 模式要求直接子元素是 Timeline.Item，
 * 自定义包装组件会破坏 antd 的 cloneElement 注入）。
 *
 * 2026-10-08 重构：原 onJump(anchor) 按钮 anchor 自指 = 永远 noop，改成
 * onDetail(payload) + 详情 Drawer；详情数据**用 ResearchCheckpoint 当前态**，
 * 不是 turn.content 的快照（检查点决策后 userChoice 写在 checkpoint 行）。
 */
import { Button, Tag, Timeline } from "antd";
import { useTranslation } from "react-i18next";
import type {
  CheckpointStatus,
  ResearchCheckpoint,
  ResearchTurn,
  TurnRole,
} from "../../types/research";

// 详情 Drawer 的入参（区分 kind 表达三种来源）。
export type ResearchDetailPayload =
  | { kind: "user"; turn: ResearchTurn }
  | { kind: "checkpoint"; turn: ResearchTurn; checkpoint: ResearchCheckpoint }
  | { kind: "orphan"; checkpoint: ResearchCheckpoint };

interface ResearchTimelineProps {
  turns: ResearchTurn[];
  checkpoints: ResearchCheckpoint[];
  onDetail: (payload: ResearchDetailPayload) => void;
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
  onDetail: (payload: ResearchDetailPayload) => void;
}

function TurnContent({ turn, checkpoint, onDetail }: TurnContentProps) {
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
        data-testid={`detail-${anchor}`}
        onClick={() => {
          if (turn.role === "user") {
            onDetail({ kind: "user", turn });
            return;
          }
          // checkpoint_awaiting 轮：若 byId 命中就用 ResearchCheckpoint 当前态；
          // 否则从 turn.content 合成一个（status=confirmed 标记「已决策、不在
          // pending 列表」、userChoice/decidedAt 不可考 = null）。这覆盖了「历史
          // 已决策 checkpoint」—— 页面只把 pendingCheckpoint 传给 timeline，
          // 历史 checkpoint 不会在 byId 里，但「查看」按钮必须有反应。
          if (turn.role === "checkpoint_awaiting") {
            const synthesized: ResearchCheckpoint = checkpoint ?? {
              id: typeof turn.content.checkpointId === "string" ? turn.content.checkpointId : turn.id,
              // 运行时 turn.content.phase 来自后端 ResearchCheckpoint.phase，
              // 必然是 CheckpointPhase 联合之一 —— 用 as 收窄即可（不是用户输入）。
              phase: (typeof turn.content.phase === "string"
                ? turn.content.phase
                : "intent") as ResearchCheckpoint["phase"],
              status: "confirmed",
              options: (turn.content.options as Record<string, unknown> | undefined) ?? {},
              prompt: typeof turn.content.prompt === "string" ? turn.content.prompt : "",
              userChoice: null,
              decidedAt: null,
            };
            onDetail({ kind: "checkpoint", turn, checkpoint: synthesized });
          }
        }}
      >
        {t("research.timeline.detail")}
      </Button>
      {text ? <p>{text}</p> : null}
    </div>
  );
}

interface CheckpointContentProps {
  checkpoint: ResearchCheckpoint;
  onDetail: (payload: ResearchDetailPayload) => void;
}

function CheckpointContent({ checkpoint, onDetail }: CheckpointContentProps) {
  const { t } = useTranslation();
  const anchor = `checkpoint-${checkpoint.id}`;
  return (
    <div id={anchor}>
      <Tag>{t(roleKey("checkpoint_awaiting"))}</Tag>
      <Tag color={STATUS_COLORS[checkpoint.status]}>{t(statusKey(checkpoint.status))}</Tag>
      <Button
        type="link"
        size="small"
        data-testid={`detail-${anchor}`}
        onClick={() => onDetail({ kind: "orphan", checkpoint })}
      >
        {t("research.timeline.detail")}
      </Button>
      {checkpoint.prompt ? <p>{checkpoint.prompt}</p> : null}
    </div>
  );
}

export function ResearchTimeline({ turns, checkpoints, onDetail }: ResearchTimelineProps) {
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
          onDetail={onDetail}
        />
      ),
    })),
    ...orphans.map((checkpoint) => ({
      key: checkpoint.id,
      children: <CheckpointContent checkpoint={checkpoint} onDetail={onDetail} />,
    })),
  ];
  return <Timeline items={items} />;
}
