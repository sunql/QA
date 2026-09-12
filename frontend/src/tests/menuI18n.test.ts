import { describe, expect, it } from "vitest";
import { zhCN } from "../i18n/zh-CN";
import { enUS } from "../i18n/en-US";

const SECTION_KEYS = [
  "aiAgent", "analytics", "bizConfig", "foundation", "systemConfig", "auditSecurity",
  // feat-wiki-knowledge：知识管理自成一级，不挂在「系统配置」下面
  "enterpriseWiki",
];
const ITEM_KEYS = [
  "chat", "agentRuntime", "agents", "supplier360", "supplierRisk",
  "ontology", "dataQuality", "lineage", "entityMapping", "kpiCatalog", "features",
  "datasource", "documents", "usage", "graph", "vectors",
  "models", "embeddings", "status", "adminAudit",
  // 企业 Wiki 的二级项 = 机制 1-6 各自的落地面板
  "wikiPages", "wikiImport", "wikiConflicts", "wikiSuggestions", "wikiCoverage",
];

describe("menu i18n keys", () => {
  it.each(SECTION_KEYS)("zh-CN has menu.section.%s", (k) => {
    expect(zhCN).toHaveProperty(`menu.section.${k}`);
  });
  it.each(SECTION_KEYS)("en-US has menu.section.%s", (k) => {
    expect(enUS).toHaveProperty(`menu.section.${k}`);
  });
  it.each(ITEM_KEYS)("zh-CN has menu.item.%s", (k) => {
    expect(zhCN).toHaveProperty(`menu.item.${k}`);
  });
  it.each(ITEM_KEYS)("en-US has menu.item.%s", (k) => {
    expect(enUS).toHaveProperty(`menu.item.${k}`);
  });
});
