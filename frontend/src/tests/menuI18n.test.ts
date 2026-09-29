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

// 上面的 ITEM_KEYS/SECTION_KEYS 是手工维护的，会随菜单增长而陈旧
// （曾漏掉 wikiCategories，导致 en-US 缺菜单文案却测试全绿）。
// 下面两条直接比对两侧键集，自维护：新增菜单项忘记补 en 文案时立即红。
describe("menu i18n 双语键集对等", () => {
  it("zh-CN 与 en-US 的 menu.section 键集完全一致", () => {
    expect(Object.keys(zhCN.menu.section).sort()).toEqual(
      Object.keys(enUS.menu.section).sort(),
    );
  });
  it("zh-CN 与 en-US 的 menu.item 键集完全一致", () => {
    expect(Object.keys(zhCN.menu.item).sort()).toEqual(
      Object.keys(enUS.menu.item).sort(),
    );
  });
});
