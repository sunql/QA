// 研究型 Agent 入口类型（feat-research-entry Task 9）。
//
// 对齐 backend/app/domain/research_schemas.py（camelCase JSON）。后端 Pydantic
// 的 `*Read` 后缀在这里去掉（前端惯例：ChatSession / WikiChatMessage 均不带 Read）。
// uuid 序列化为字符串、datetime 序列化为 ISO 字符串。

export type ResearchMode = "research" | "attribution" | "compare";
export type TurnRole = "user" | "agent" | "checkpoint_awaiting";
export type CheckpointPhase =
  | "intent"
  | "planning"
  | "hypothesis"
  | "runtime_dynamic"
  | "low_confidence_step";
export type SessionStatus = "running" | "awaiting_user" | "done" | "failed" | "aborted";
export type CheckpointStatus = "pending" | "confirmed" | "modified" | "rejected";
export type ReportStatus = "published" | "superseded";
export type CheckpointAction = "confirm" | "modify" | "reject";

export interface ResearchSession {
  id: string;
  title: string;
  mode: ResearchMode;
  status: SessionStatus;
  question: string;
  datasourceId?: number | null;
  modelId?: number | null;
  createdAt: string;
  updatedAt: string;
}

export interface ResearchTurn {
  id: string;
  turnIndex: number;
  role: TurnRole;
  content: Record<string, unknown>;
  createdAt: string;
}

export interface ResearchCheckpoint {
  id: string;
  phase: CheckpointPhase;
  status: CheckpointStatus;
  options: Record<string, unknown>;
  prompt: string;
  userChoice: Record<string, unknown> | null;
  decidedAt: string | null;
}

export interface ResearchSessionDetail {
  session: ResearchSession;
  turns: ResearchTurn[];
  pendingCheckpoint: ResearchCheckpoint | null;
}

export interface ResearchTurnAccepted {
  sessionId: string;
  turnId: string;
  status: SessionStatus;
}

export interface CheckpointAnswer {
  sessionStatus: SessionStatus;
  nextPhase: string;
}

export interface ResearchReport {
  id: string;
  version: number;
  status: ReportStatus;
  payload: Record<string, unknown>;
  renderedMd: string;
  createdAt: string;
}

export interface ResearchReportSummary {
  id: string;
  version: number;
  status: ReportStatus;
  createdAt: string;
}

// SSE 事件名全集（对齐 backend/services/research_agent_ports.py 的 EVENT_* 常量
// + research_event_bus.py 的 EVENT_CONNECTED）。事件名本身是语义，必须按名分发。
export type ResearchEventName =
  | "research.connected"
  | "research.intent"
  | "research.esl"
  | "research.checkpoint"
  | "research.plan"
  | "research.step.start"
  | "research.step.sql"
  | "research.step.data"
  | "research.step.chart"
  | "research.step.done"
  | "research.hypothesis"
  | "research.finding"
  | "research.report"
  | "research.done"
  | "research.error";

export interface ResearchSseEvent {
  name: ResearchEventName;
  payload: Record<string, unknown>;
}

// research.error 的处置分类（用户裁定规则 ③）：前端只按 uiHint 的「类」分支，
// 不按 code 逐个判断（后端新增终态 code 时前端零改动）。uiHint 与 code/message
// 同级恒在，由后端 ERROR_SPECS 表派生。
export type ResearchErrorUiHint = "terminal" | "degraded";

export interface ResearchErrorPayload {
  code: string;
  message?: string;
  uiHint: ResearchErrorUiHint;
}
