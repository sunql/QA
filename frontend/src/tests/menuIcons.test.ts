import { describe, expect, it } from "vitest";
import { ICON_REGISTRY, renderIcon } from "../components/common/menuIcons";

describe("ICON_REGISTRY", () => {
  it("contains all 21 codes referenced by seed", () => {
    const expected = [
      "robot", "fund", "setting", "database", "api", "safety",
      "message", "thunderbolt", "appstore",
      "barchart", "alert",
      "partition", "audit", "node", "code", "number", "cluster",
      "file", "dashboard", "apartment", "heart",
    ];
    for (const code of expected) {
      expect(ICON_REGISTRY).toHaveProperty(code);
      expect(typeof ICON_REGISTRY[code]).toBe("function");
    }
  });
});

describe("renderIcon", () => {
  it("returns null for undefined", () => {
    expect(renderIcon(undefined)).toBeNull();
  });
  it("returns null for unknown code (no crash)", () => {
    expect(renderIcon("nonexistent")).toBeNull();
  });
  it("returns a React node for known code", () => {
    const node = renderIcon("robot");
    expect(node).not.toBeNull();
  });
});
