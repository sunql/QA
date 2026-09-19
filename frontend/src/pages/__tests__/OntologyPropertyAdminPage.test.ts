import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const src = readFileSync(
  join(process.cwd(), "src/pages/OntologyPropertyAdminPage.tsx"),
  "utf-8"
);

const appTsx = readFileSync(join(process.cwd(), "src/App.tsx"), "utf-8");

const seedMenu = readFileSync(
  join(process.cwd(), "../backend/scripts/seed_menu_config.py"),
  "utf-8"
);

const zhSrc = readFileSync(
  join(process.cwd(), "src/i18n/zh-CN.ts"),
  "utf-8"
);

const enSrc = readFileSync(
  join(process.cwd(), "src/i18n/en-US.ts"),
  "utf-8"
);

describe("OntologyPropertyAdminPage wiring", () => {
  it("renders a standalone page at /ontology-properties", () => {
    expect(appTsx).toMatch(/path="ontology-properties"[^>]*element=\{<OntologyPropertyAdminPage \/>/);
    expect(src).toContain("export default function OntologyPropertyAdminPage");
  });

  it("uses listAllProperties + updateProperty for list & edit (no N+1)", () => {
    // 必须用全局端点 GET /ontology/properties，避免循环调 listPropertiesByClass 做 N+1
    expect(src).toMatch(/listAllProperties/);
    expect(src).toMatch(/updateProperty/);
  });

  it("renders allowedValues column as Tags (visible affordance for persisted state)", () => {
    // 列渲染必须基于 allowedValues 字段用 Tag 渲染，否则用户看不到「我采纳的值」
    expect(src).toMatch(/allowedValues/);
    expect(src).toMatch(/<Tag/);
  });

  it("edit modal lets user adjust allowedValues via tags input", () => {
    expect(src).toMatch(/mode="tags"/);
    expect(src).toMatch(/allowedValues: string\[\]/);
  });

  it("menu seed registers item.ontologyProperties pointing to /ontology-properties", () => {
    // 同 seed 的其他条目模式：code + label_key + path 在同一 dict 内。
    expect(seedMenu).toContain('"item.ontologyProperties"');
    expect(seedMenu).toContain('"menu.item.ontologyProperties"');
    expect(seedMenu).toContain('"/ontology-properties"');
    // 三个字段必须在同一个 dict literal（避免三个 key 散落在文件各处造成 false-positive）
    expect(seedMenu).toMatch(
      /\{[^}]*"item\.ontologyProperties"[^}]*"menu\.item\.ontologyProperties"[^}]*"\/ontology-properties"[^}]*\}/
    );
  });

  it("i18n zh-CN defines menu + page strings", () => {
    expect(zhSrc).toMatch(/ontologyProperties:\s*["']本体属性管理["']/);
    expect(zhSrc).toMatch(/ontologyPropertyAdmin:\s*\{/);
    expect(zhSrc).toMatch(/title:\s*["']本体属性管理["']/);
    expect(zhSrc).toMatch(/loadFailed:\s*["']加载本体属性失败["']/);
  });

  it("i18n en-US defines menu + page strings", () => {
    expect(enSrc).toMatch(/ontologyProperties:\s*["']Ontology Property Management["']/);
    expect(enSrc).toMatch(/ontologyPropertyAdmin:\s*\{/);
    expect(enSrc).toMatch(/title:\s*["']Ontology Property Management["']/);
    expect(enSrc).toMatch(/loadFailed:\s*["']Failed to load ontology properties["']/);
  });
});
describe("OntologyPropertyAdminPage 编辑回填与描述可见性", () => {
  it("openEdit 必须回填现有 description（硬编码空串会让用户以为保存失败）", () => {
    // 2026-09-18 报障：编辑弹窗打开永远为空 → 用户保存成功后重开仍为空 → 以为没存上
    expect(src).toMatch(/description:\s*rec\.description/);
  });

  it("表格必须渲染 description 列（保存结果对用户可见）", () => {
    expect(src).toMatch(/dataIndex:\s*"description"/);
  });
});
