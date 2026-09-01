/**
 * fallbackNav - 当 /menu-config API 失败时的兜底菜单。
 *
 * 严格复制自原 AppLayout 的 NAV_KEYS，保留全部 20 项。
 * 一旦本期上线稳定，可移除此文件。
 */
export const FALLBACK_NAV: readonly { key: string; labelKey: string }[] = [
  { key: "/models", labelKey: "appLayout.menu.models" },
  { key: "/embeddings", labelKey: "appLayout.menu.embeddings" },
  { key: "/chat", labelKey: "appLayout.menu.chat" },
  { key: "/ontology", labelKey: "appLayout.menu.ontology" },
  { key: "/datasource", labelKey: "appLayout.menu.datasource" },
  { key: "/data-quality", labelKey: "appLayout.menu.dataQuality" },
  { key: "/lineage", labelKey: "appLayout.menu.lineage" },
  { key: "/entity-mapping", labelKey: "appLayout.menu.entityMapping" },
  { key: "/kpi-catalog", labelKey: "appLayout.menu.kpiCatalog" },
  { key: "/features", labelKey: "appLayout.menu.features" },
  { key: "/usage", labelKey: "appLayout.menu.usage" },
  { key: "/status", labelKey: "appLayout.menu.status" },
  { key: "/graph", labelKey: "appLayout.menu.graph" },
  { key: "/vectors", labelKey: "appLayout.menu.vectors" },
  { key: "/supplier-360", labelKey: "appLayout.menu.supplier360" },
  { key: "/supplier-risk", labelKey: "appLayout.menu.supplierRisk" },
  { key: "/agents/run", labelKey: "appLayout.menu.agentRuntime" },
  { key: "/agents", labelKey: "appLayout.menu.agents" },
  { key: "/documents", labelKey: "appLayout.menu.documents" },
  { key: "/admin/audit", labelKey: "appLayout.menu.adminAudit" },
] as const;
