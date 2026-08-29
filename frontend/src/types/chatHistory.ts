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
}

// 会话消息流响应（对齐后端 SessionMessagesResponse）
export interface SessionMessagesResponse {
  sessionId: string;
  messages: ChatMessageRead[];
}