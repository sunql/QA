import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const src = readFileSync(
  join(process.cwd(), "src/pages/OntologyPropertyAdminPage.tsx"),
  "utf-8"
);

const typesSrc = readFileSync(
  join(process.cwd(), "src/types/ontology.ts"),
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
 * feat-ontology-property-constraints: 4 个约束字段在本体管理编辑弹窗里必须可维护。
 *
 * 背景：applySuggestion 服务写入 ontology_property.is_not_null / min_value /
 * max_value / regex_pattern 必须有手工回退入口，否则用户只能通过 LLM 采纳沉淀
 * 这四个约束，没办法独立维护。
 *
 * 契约（必须全部满足）：
 *  1. EditFormValues 类型包含 isNotNull / minValue / maxValue / regexPattern
 *  2. 弹窗 Form 里分别用 Switch / InputNumber / InputNumber / Input 渲染
 *  3. openEdit 把 record 的 4 个字段 set 进 form
 *  4. onSubmit 把 form 值通过 OntologyPropertyUpdate payload 提交（类型层面）
 *  5. i18n zh-CN / en-US 都定义 4 个 label/hint key
 *  6. OntologyProperty / Update 类型包含同名 4 字段（与后端 camelCase 契约对齐）
 */
describe("OntologyPropertyAdminPage constraint-fields contract", () => {
  it("EditFormValues interface declares the 4 constraint fields", () => {
    expect(src).toMatch(/interface\s+EditFormValues[\s\S]*?isNotNull[\s\S]*?minValue[\s\S]*?maxValue[\s\S]*?regexPattern[\s\S]*?\}/);
  });

  it("renders isNotNull as Switch with valuePropName='checked'", () => {
    // Switch 控件 + valuePropName="checked" 是 antd 标准写法。
    expect(src).toMatch(/name="isNotNull"[\s\S]{0,200}<Switch/);
    expect(src).toMatch(/valuePropName="checked"/);
  });

  it("renders minValue and maxValue as InputNumber (numeric input)", () => {
    // range 约束是数字或日期字符串，前端用 InputNumber 录入数字。
    expect(src).toMatch(/name="minValue"[\s\S]{0,200}<InputNumber/);
    expect(src).toMatch(/name="maxValue"[\s\S]{0,200}<InputNumber/);
  });

  it("renders regexPattern as Input (string)", () => {
    // 正则表达式是字符串，前端用 Input + allowClear。
    expect(src).toMatch(/name="regexPattern"[\s\S]{0,200}<Input/);
  });

  it("openEdit pre-populates the 4 constraint fields from the record", () => {
    // setFieldsValue 必须把 record.isNotNull / minValue / maxValue / regexPattern 传进去。
    expect(src).toMatch(/isNotNull:\s*rec\.isNotNull/);
    expect(src).toMatch(/minValue:\s*toNum\(rec\.minValue\)/);
    expect(src).toMatch(/maxValue:\s*toNum\(rec\.maxValue\)/);
    expect(src).toMatch(/regexPattern:\s*rec\.regexPattern/);
  });

  it("onSubmit sends the 4 fields inside OntologyPropertyUpdate payload", () => {
    // payload: OntologyPropertyUpdate = { ..., isNotNull, minValue, maxValue, regexPattern }
    const payloadLiteral = src.match(
      /const\s+payload:\s*OntologyPropertyUpdate\s*=\s*\{[\s\S]*?\};/
    );
    expect(payloadLiteral, "OntologyPropertyUpdate payload literal missing").not.toBeNull();
    expect(payloadLiteral![0]).toMatch(/isNotNull:/);
    expect(payloadLiteral![0]).toMatch(/minValue:\s*numToStr/);
    expect(payloadLiteral![0]).toMatch(/maxValue:\s*numToStr/);
    expect(payloadLiteral![0]).toMatch(/regexPattern:/);
  });

  it("OntologyProperty + OntologyPropertyUpdate types declare the 4 camelCase fields", () => {
    // 后端 schema 字段是 isNotNull/minValue/maxValue/regexPattern（camelCase JSON 契约）。
    // 前端类型必须同步，否则 type-check 失败 + 序列化时不带字段。
    expect(typesSrc).toMatch(/isNotNull\??:\s*boolean\s*\|\s*null/);
    expect(typesSrc).toMatch(/minValue\??:\s*string\s*\|\s*null/);
    expect(typesSrc).toMatch(/maxValue\??:\s*string\s*\|\s*null/);
    expect(typesSrc).toMatch(/regexPattern\??:\s*string\s*\|\s*null/);
  });

  it("i18n zh-CN defines 4 label keys (isNotNull/minValue/maxValue/regexPattern)", () => {
    expect(zhSrc).toMatch(/isNotNull:\s*["']/);
    expect(zhSrc).toMatch(/minValue:\s*["']/);
    expect(zhSrc).toMatch(/maxValue:\s*["']/);
    expect(zhSrc).toMatch(/regexPattern:\s*["']/);
    // 每个字段都必须有 hint（tooltip 必有）
    expect(zhSrc).toMatch(/isNotNullHint:\s*["']/);
    expect(zhSrc).toMatch(/minValueHint:\s*["']/);
    expect(zhSrc).toMatch(/maxValueHint:\s*["']/);
    expect(zhSrc).toMatch(/regexPatternHint:\s*["']/);
  });

  it("i18n en-US defines 4 label keys (isNotNull/minValue/maxValue/regexPattern)", () => {
    expect(enSrc).toMatch(/isNotNull:\s*["']/);
    expect(enSrc).toMatch(/minValue:\s*["']/);
    expect(enSrc).toMatch(/maxValue:\s*["']/);
    expect(enSrc).toMatch(/regexPattern:\s*["']/);
    expect(enSrc).toMatch(/isNotNullHint:\s*["']/);
    expect(enSrc).toMatch(/minValueHint:\s*["']/);
    expect(enSrc).toMatch(/maxValueHint:\s*["']/);
    expect(enSrc).toMatch(/regexPatternHint:\s*["']/);
  });
});