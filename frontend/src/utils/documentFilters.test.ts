import { describe, expect, it } from "vitest";
import { documentFilterFields } from "./documentFilters";

describe("documentFilterFields", () => {
  it("类型/状态下拉都带非空 options（否则展开是空列表）", () => {
    const fields = documentFilterFields();
    const typeField = fields.find((field) => field.key === "documentType");
    const statusField = fields.find((field) => field.key === "status");

    expect(typeField?.options?.length).toBeGreaterThan(0);
    expect(typeField?.options?.some((option) => option.value === "SOP")).toBe(true);
    expect(statusField?.options?.map((option) => option.value)).toEqual([
      "ACTIVE",
      "EXPIRED",
      "ARCHIVED",
    ]);
  });
});
