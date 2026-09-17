// Wiki Chat 类型（feat-wiki-chat，对齐后端 WikiQaService SSE 事件与 citation 契约）

// 引用条目（对齐 searchSemantic hit + 1-based id；camelCase JSON）
export interface WikiCitation {
  id: number;
  pageId: string;
  title: string;
  status?: string;
  dimension?: string | null;
  chunkText?: string;
  chunkSequence?: number | null;
  distance?: number;
  score: number;
}

// 页面内消息（citations 仅 assistant 行填充）
export interface WikiChatMessage {
  role: "user" | "assistant";
  content: string;
  citations?: WikiCitation[] | null;
}

// SSE 事件 kind（对齐 qa_meta / qa_citations / token / qa_done / error）
export type WikiChatEventKind = "meta" | "citations" | "token" | "done" | "error";

export interface WikiChatSseEvent {
  kind: WikiChatEventKind;
  intent?: string;
  citations?: WikiCitation[];
  content?: string;
  tokensUsed?: number;
  cost?: number;
  modelName?: string | null;
  error?: string;
  errorType?: string;
}

export interface WikiChatRequestPayload {
  sessionId: string;
  question: string;
  topK?: number;
  dimension?: string;
  modelId?: number;
}
