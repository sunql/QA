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

  it("shows explicit warning when clicking adopt on non-applicable item (no silent return)", () => {
    // 回归：handleApply 早返条件 `kind !== 'allowed_values' || !item.values`
    // 对 not_null 建议或 values=null 的 allowed_values 建议是 silent no-op，
    // 用户点击按钮无任何反馈（无 message、无 error、按钮不变），后端也不会被调用。
    // 修复契约：早返前必须 message.warning(...) + i18n key，且不得 return 静默。
    expect(src).toMatch(/kind\s*[!=]==\s*["']allowed_values["']/);
    // 早返前必须有 message.warning 调用（不能 return 静默）
    expect(src).toMatch(/message\.warning/);
    // i18n 文本必须存在（zh-CN + en-US 双语）
    expect(zhSrc).toMatch(/adoptNotApplicable|adoptOnlyAllowedValues|adoptKindNotSupported/);
    expect(enSrc).toMatch(/adoptNotApplicable|adoptOnlyAllowedValues|adoptKindNotSupported/);
  });

  it("pre-populates adoptedIds from parse-descriptions persistedPropertyIds (refresh-survives)", () => {
    // 回归：adoptedIds 初始化为空 Set → 刷新页面清零 → 用户误以为「再操作就不行了」。
    // 修复契约：load 成功回调必须从 parseDescriptions 返回的 persistedPropertyIds
    // 把已沉淀的 propertyId 加入 adoptedIds。
    expect(src).toMatch(/persistedPropertyIds/);
    // 必须有 setAdoptedIds 派生逻辑，把 result.persistedPropertyIds 加进去
    expect(src).toMatch(/setAdoptedIds[\s\S]{0,200}persistedPropertyIds/);
  });

  it("shows message.info when re-adopting an already-adopted item (no silent return)", () => {
    // 回归：handleApply 早返条件 `adoptedIds.has(item.propertyId)` 是 silent no-op，
    // 用户再点按钮无任何反馈（按钮 disabled 但点击事件仍触发，无 message）。
    // 修复契约：早返前必须 message.info(...) + i18n key，且不得 return 静默。
    expect(src).toMatch(/adoptedIds\.has\(item\.propertyId\)[\s\S]{0,200}message\.info/);
    // i18n 文本必须存在（zh-CN + en-US 双语）
    expect(zhSrc).toMatch(/alreadyAdopted:\s*["']此属性已采纳/);
    expect(enSrc).toMatch(/alreadyAdopted:\s*["']This property has already been adopted["']/);
  });

  it("uses composite key (propertyId-kind) for items.map to avoid React duplicate key warning", () => {
    // 回归：items.map() 用 key={item.propertyId} 时，LLM 给同一 property 输出多条
    // 不同 kind 的合法建议（not_null + allowed_values）会撞 React "duplicate key" 警告。
    // 后端已按 (propertyId, kind) 去重，但前端 key 仍必须复合，作为双保险。
    expect(src).toMatch(/key=\{`\$\{item\.propertyId\}-\$\{item\.kind\}`\}/);
  });

  // -----------------------------------------------------------------------
  // feat-ontology-property-constraints: 4 类 kind 派发契约
  // （plan §改动 4 — handleApply 不再早返回非 allowed_values，按 kind 派发）
  // -----------------------------------------------------------------------

  it("removes the legacy `kind !== allowed_values` early-return so all 4 kinds are dispatched", () => {
    // 回归：handleApply 早返条件 `kind !== "allowed_values" || !item.values`
    // 让 not_null / range / pattern 三类建议被 silent skip。修复后 dispatch
    // 由 buildApplyPayload 按 kind 派发，原早返条件不再存在。
    // 注意：源码仍有 `kind === "allowed_values"` 用于渲染分支（line 382 tags 显示），
    // 所以这里精确校验"handleApply 内"是否还存在 `kind !== "allowed_values"`。
    const handleApplyMatch = src.match(/const\s+handleApply\s*=\s*async[\s\S]*?^\s*\};/m);
    expect(handleApplyMatch, "handleApply function literal missing").not.toBeNull();
    expect(handleApplyMatch![0]).not.toMatch(/kind\s*!==\s*["']allowed_values["']/);
    // buildApplyPayload 必须在 handleApply 之前/同文件可见位置（不依赖 import）。
    expect(src).toContain("buildApplyPayload");
  });

  it("routes not_null kind to applySuggestion with kind:'not_null' and no extra fields", () => {
    // not_null 派发：payload = { kind: "not_null" }，不传 allowedValues/minValue/maxValue/regexPattern
    expect(src).toMatch(/kind:\s*["']not_null["']/);
    // 锚定到 case 结尾的下一个 case（range），避免跨 case 污染
    const notNullBranch = src.match(
      /case\s+["']not_null["'][\s\S]*?(?=case\s+["']range["'])/
    );
    expect(notNullBranch, "not_null switch case missing").not.toBeNull();
    // not_null 不带其它约束字段
    expect(notNullBranch![0]).not.toMatch(/allowedValues|minValue|maxValue|regexPattern/);
  });

  it("routes range kind to applySuggestion with kind:'range' + minValue + maxValue", () => {
    // range 派发：payload = { kind: "range", minValue, maxValue }
    expect(src).toMatch(/kind:\s*["']range["']/);
    const rangeBranch = src.match(/case\s+["']range["'][\s\S]{0,300}/);
    expect(rangeBranch, "range switch case missing").not.toBeNull();
    expect(rangeBranch![0]).toMatch(/minValue/);
    expect(rangeBranch![0]).toMatch(/maxValue/);
    // range 必备字段缺失时返回 kind=null（不发送请求）
    expect(rangeBranch![0]).toMatch(/kind:\s*null/);
  });

  it("routes pattern kind to applySuggestion with kind:'pattern' + regexPattern", () => {
    // pattern 派发：payload = { kind: "pattern", regexPattern }
    expect(src).toMatch(/kind:\s*["']pattern["']/);
    const patternBranch = src.match(/case\s+["']pattern["'][\s\S]{0,300}/);
    expect(patternBranch, "pattern switch case missing").not.toBeNull();
    expect(patternBranch![0]).toMatch(/regexPattern/);
    // regex_pattern 缺失时返回 kind=null（不发送请求）
    expect(patternBranch![0]).toMatch(/kind:\s*null/);
  });

  it("api applySuggestion signature accepts discriminated kind payload (4 kinds)", () => {
    // 对应 plan §改动 3.3: applySuggestion(propertyId, payload) 的 payload
    // 是按 kind 派发的 discriminated object，不允许传 null/undefined kind。
    const apiSrc = readFileSync(
      join(process.cwd(), "src/api/dataQualityGenerate.ts"),
      "utf-8"
    );
    // payload 字段名匹配 SuggestionKind 联合（至少出现 allowed_values / not_null / range / pattern 4 个）
    expect(apiSrc).toMatch(/allowed_values/);
    expect(apiSrc).toMatch(/not_null/);
    expect(apiSrc).toMatch(/range/);
    expect(apiSrc).toMatch(/pattern/);
    // POST body 必须带 propertyId + kind + 命中的额外字段
    expect(apiSrc).toMatch(/body\.kind/);
    expect(apiSrc).toMatch(/body\.allowedValues/);
    expect(apiSrc).toMatch(/body\.minValue/);
    expect(apiSrc).toMatch(/body\.maxValue/);
    expect(apiSrc).toMatch(/body\.regexPattern/);
  });

  it("buildApplyPayload warns via message.warning when LLM output is incomplete (kind=null)", () => {
    // 派发 payload.kind === null 时必须有 message.warning 提示，不能 silent return
    // （已有覆盖，但为新派发路径再校验一次：applyFailed 文案 vs adoptNotApplicable 文案）
    expect(src).toMatch(/payload\.kind\s*===\s*null[\s\S]{0,200}message\.warning/);
    expect(zhSrc).toMatch(/adoptNotApplicable:\s*["']/);
    expect(enSrc).toMatch(/adoptNotApplicable:\s*["']/);
  });
});