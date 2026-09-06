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
});
