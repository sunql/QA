import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const src = readFileSync(
  join(process.cwd(), "src/pages/DataQualityRuleGeneratePage.tsx"),
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

/**
 * 回归测试：LlmPanel「采纳并沉淀」按钮点击后必须有可见的视觉反馈。
 *
 * 背景：applySuggestion 写入 ontology_property.allowed_values 返回 200，但
 * 面板内的该条建议仍然显示为可点击状态，用户不知道已采纳。
 *
 * 修复契约（必须全部满足）：
 *  1. LlmPanel 用 Set<number> 本地跟踪已采纳的 propertyId
 *  2. handleApply 成功后将该 propertyId 加入集合
 *  3. 已采纳的按钮 disabled，文字改为 "已采纳"
 *  4. 面板 header 出现绿色 "已沉淀" 提示
 *  5. i18n zh-CN/en-US 都定义了 adopted / adoptedHint key
 */
describe("LlmPanel adopt-and-persist UI feedback", () => {
  it("tracks adopted propertyIds locally so UI reflects persisted state", () => {
    expect(src).toMatch(/adoptedIds/);
    expect(src).toMatch(/setAdoptedIds/);
  });

  it("disables the adopt button after successful apply", () => {
    // Button 的 disabled 条件必须由 adoptedIds 推导（直接或通过 isAdopted 派生）。
    // 接受两种写法：`disabled={adoptedIds.has(...)}` 或先 const isAdopted = adoptedIds.has(...) 再 disabled={isAdopted}。
    const direct = /disabled=\{adoptedIds\.has\(item\.propertyId\)\}/;
    const derived = /const\s+isAdopted\s*=\s*adoptedIds\.has\(item\.propertyId\)[\s\S]*?disabled=\{isAdopted\}/;
    expect(src.match(direct) || src.match(derived), "no adoptedIds-driven disabled branch").not.toBeNull();
  });

  it("changes button label to '已采纳' once the property has been adopted", () => {
    // 按钮文字条件渲染：已采纳 → t("dataQualityGenerate.adopted")，否则 → adopt
    expect(src).toContain("dataQualityGenerate.adopted");
    // 必须存在一个三元或条件分支把"已采纳/Adopted"渲染出来。
    const ternary =
      src.match(/isAdopted\s*\?[^:]+adoptedHint?[^:]+:[^"]+adopt"/) ||
      src.match(/adoptedIds\.has\(item\.propertyId\)\s*\?[^:]+adopted[^:]+:[^"]+adopt"/) ||
      src.match(/isAdopted\s*\?[^:]+adopted["']/);
    expect(ternary, "adopted button label branch not found").not.toBeNull();
  });

  it("shows a '已沉淀' hint in the item header after adoption", () => {
    // 已采纳后该 item 的 header 区域出现 adoptedHint 提示
    expect(src).toContain("dataQualityGenerate.adoptedHint");
    // 提示必须受 adoptedIds 控制显隐（仅已采纳时显示）
    expect(src).toMatch(/adoptedIds\.has\(item\.propertyId\).*adoptedHint/s);
  });

  it("i18n zh-CN defines adopted + adoptedHint labels", () => {
    expect(zhSrc).toMatch(/adopted:\s*["']已采纳["']/);
    expect(zhSrc).toMatch(/adoptedHint:\s*["']已沉淀到本体属性["']/);
  });

  it("i18n en-US defines adopted + adoptedHint labels", () => {
    expect(enSrc).toMatch(/adopted:\s*["']Adopted["']/);
    expect(enSrc).toMatch(/adoptedHint:\s*["']Persisted to ontology property["']/);
  });

  it("still calls onApplied() so upstream preview refreshes", () => {
    // onApplied 是把 preview 重新拉一遍的关键，不能因为 UI 反馈而跳过
    expect(src).toMatch(/onApplied\(\)/);
  });
});