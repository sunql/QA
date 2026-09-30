// 聊天会话历史面板相关类型（对齐后端 ChatSessionListItem / ChatMessageRead / SessionMessagesResponse）

// 聊天会话列表项（对齐后端 ChatSessionListItem，CamelModel → camelCase JSON）
export interface ChatSession {
  sessionId: string;
  firstTime: string;
  lastTime: string;
  messageCount: number;
  lastQuestion: string | null;
  lastAnswerPreview: string | null;
}

// 单条历史消息（对齐后端 ChatMessageRead）
export interface ChatMessageRead {
  id: number;
  role: "user" | "assistant";
  content: string;
  // 仅 user 行填充；assistant 行为 null
  question: string | null;
  // 仅 assistant 行填充；user 行为 null
  sql: string | null;
  createdTime: string;
  // H4：assistant 行由断连兜底写入（content 可能是半截回答，也可能是空产出占位文案）；
  // user 行恒 false
  interrupted: boolean;
  // 0105（图表进最终报告）：该轮回答的图表负载。此前后端没持久化这两个字段，
  // 切走再切回整段图消失；现在历史回放也能出图。
  // 为什么是 unknown 而不是 ChartType：这是**系统边界**上的原始 JSON，落库时可能
  // 来自更早版本的后端。由 chatStore 过 normalizeChartType 白名单收窄，不在这里硬 cast。
  chartType?: unknown;
  chartOption?: unknown;
}

// 会话消息流响应（对齐后端 SessionMessagesResponse）
export interface SessionMessagesResponse {
  sessionId: string;
  messages: ChatMessageRead[];
}