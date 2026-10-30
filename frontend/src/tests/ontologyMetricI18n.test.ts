/**
 * 指标技术形态的 i18n key 存在性闸门。
 *
 * `t()` 的签名是 `(key: string) => string`，缺 key 时**返回 key 本身** ⇒
 * 拼错的模板字面量（如 `enums.metricType.RATI0`）在页面上只是显示原始 key，
 * 不报错、不失败。本文件遍历 `*_OPTIONS` 与手写的 label / column / placeholder
 * key，断言中文与英文词典都存在非空译文。
 */
import { describe, it, expect } from "vitest";
import { zhCN, enUS } from "../i18n";
import {
  METRIC_AGGREGATION_ORDER_OPTIONS,
  METRIC_EXECUTION_SOURCE_OPTIONS,
  METRIC_STATUS_OPTIONS,
  METRIC_TYPE_OPTIONS,
} from "../types/ontology";

type Dictionary = Record<string, unknown>;

const DICTIONARIES: [string, Dictionary][] = [
  ["zh-CN", zhCN as unknown as Dictionary],
  ["en-US", enUS as unknown as Dictionary],
];

const ENUM_GROUPS: { group: string; options: { labelKey: string }[] }[] = [
  { group: "metricType", options: METRIC_TYPE_OPTIONS },
  { group: "metricAggregationOrder", options: METRIC_AGGREGATION_ORDER_OPTIONS },
  { group: "metricExecutionSource", options: METRIC_EXECUTION_SOURCE_OPTIONS },
  { group: "metricStatus", options: METRIC_STATUS_OPTIONS },
];

const LABEL_KEYS = [
  "metricType",
  "aggregationOrder",
  "executionSource",
  "numeratorAgg",
  "numeratorExpr",
  "numeratorFilter",
  "denominatorAgg",
  "denominatorExpr",
  "denominatorFilter",
  "grainKeys",
  "filterExpression",
  "owner",
  "status",
];

const COLUMN_KEYS = ["metricTypeColumn", "aggregationOrderColumn", "executionSourceColumn"];

const PLACEHOLDER_KEYS = [
  "numeratorExpr",
  "numeratorFilter",
  "denominatorExpr",
  "denominatorFilter",
  "grainKeys",
  "filterExpression",
  "owner",
];

function lookup(dict: Dictionary, path: string[]): unknown {
  return path.reduce<unknown>((node, key) => {
    if (node === null || typeof node !== "object") return undefined;
    return (node as Dictionary)[key];
  }, dict);
}

function expectNonEmpty(dict: Dictionary, path: string[]): void {
  const key = path.join(".");
  const value = lookup(dict, path);
  expect(typeof value, `缺失 i18n key: ${key}`).toBe("string");
  expect((value as string).trim().length, `i18n key 为空: ${key}`).toBeGreaterThan(0);
}

describe.each(DICTIONARIES)("%s — 指标技术形态 i18n", (_locale, dict) => {
  it.each(ENUM_GROUPS)("枚举 $group 的每个成员都有非空译文", ({ group, options }) => {
    for (const option of options) {
      expectNonEmpty(dict, ["enums", group, option.labelKey]);
    }
  });

  it("forms.ontology.metricLabels 的 13 个 label 都有非空译文", () => {
    for (const key of LABEL_KEYS) {
      expectNonEmpty(dict, ["forms", "ontology", "metricLabels", key]);
    }
  });

  it("forms.ontology.metricColumns 的 3 个 column 都有非空译文", () => {
    for (const key of COLUMN_KEYS) {
      expectNonEmpty(dict, ["forms", "ontology", "metricColumns", key]);
    }
  });

  it("forms.ontology.metricPlaceholders 的 7 个 placeholder 都有非空译文", () => {
    for (const key of PLACEHOLDER_KEYS) {
      expectNonEmpty(dict, ["forms", "ontology", "metricPlaceholders", key]);
    }
  });

  it("forms.ontology.metricHints.publishGateNotice 存在且非空", () => {
    expectNonEmpty(dict, ["forms", "ontology", "metricHints", "publishGateNotice"]);
  });
});
