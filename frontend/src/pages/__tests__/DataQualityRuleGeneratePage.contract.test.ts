import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const src = readFileSync(
  join(process.cwd(), "src/pages/DataQualityRuleGeneratePage.tsx"),
  "utf-8"
);

describe("DataQualityRuleGeneratePage contract", () => {
  it("renders 4 wizard steps using i18n keys", () => {
    for (const key of ["stepClass", "stepDatasource", "stepPreview", "stepConfirm"]) {
      expect(src).toContain(`dataQualityGenerate.${key}`);
    }
  });

  it("calls confirmRules only with NEW suggestions", () => {
    expect(src).toContain('status === "NEW"');
  });

  it("guards handleConfirm when no NEW rule is selected (no 422 on empty array)", () => {
    // 回归守护：之前 handleConfirm 直接调 confirmRules(..., toSubmit)，
    // 当用户勾选的都是 status="EXISTS" 规则时，toSubmit=[] → POST rules=[] →
    // 后端 min_length=1 → 422 Unprocessable Entity，用户看到 422 但不知为何。
    // 修复：toSubmit.length===0 时 message.warning(...) + early return。
    expect(src).toMatch(/toSubmit\.length\s*===\s*0/);
    expect(src).toMatch(/message\.warning/);
  });

  it("disables confirm button when no NEW rule is selected", () => {
    // 按钮 disabled={toSubmit.length === 0}（不是 selectedIds.size，因为
    // selectedIds 可能含 EXISTS 规则的勾选）。
    expect(src).toMatch(/disabled=\{[^}]*toSubmit\.length[^}]*===[^}]*0/);
  });
});
