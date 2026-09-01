import { describe, expect, it } from "vitest";
import { zhCN } from "../i18n/zh-CN";
import { enUS } from "../i18n/en-US";

const SECTION_KEYS = [
  "aiAgent", "analytics", "bizConfig", "foundation", "systemConfig", "auditSecurity",
];
const ITEM_KEYS = [
  "chat", "agentRuntime", "agents", "supplier360", "supplierRisk",
  "ontology", "dataQuality", "lineage", "entityMapping", "kpiCatalog", "features",
  "datasource", "documents", "usage", "graph", "vectors",
  "models", "embeddings", "status", "adminAudit",
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
