// Agent 运行时（Phase 6.4 feat-agent-runtime-mvp）
// 对齐后端 AgentRunRead。result 为 Tool 原始输出（camelCase，JSON-serializable），
// 具体形状按 tool 名路由：
//   supplier_360   → Supplier360Read（可复用 Supplier360Card）
//   supplier_risk  → SupplierRiskRead（可复用 SupplierRiskCard）
//   graph_traverse → GraphTraversalRead（可复用 GraphTraversalCard）
// answer 为工具 handler 生成的人类可读回答（与后端 buildRiskAnswer 同源）。

export interface AgentRunRead {
  agentCode: string;
  agentName: string;
  agentOwner: string | null;
  tool: string;
  result: Record<string, unknown>;
  answer: string;
  tokensUsed: number;
  cost: number;
  llmModelName: string | null;
  executedAt: string;
}
