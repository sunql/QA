/**
 * KPI 数据源建议（datasource-suggestions）的 i18n 四端一致性闸门。
 *
 * `t()` 的签名是 `(key: string, vars?: Vars) => string`，**没有 key 联合类型
 * 保护** ⇒ 缺 key 时 `t()` 只是返回 key 本身：不报错、tsc 也不报错，
 * 页面上直接渲染裸 key `kpiCatalog.suggestions.reasons.xxx`。
 *
 * ⇒ 只能在测试层兜底：每个 reason（TS 联合 `KpiDatasourceSuggestionReason`）
 * 都必须在中英词典里存在且译文非空；两侧的 reason 集合也必须一致
 * （防止只加了一边）。
 *
 * 注意：**不要改 `frontend/src/i18n/zh-CN.ts` / `en-US.ts` 词典本体** ——
 * 新增 reason 时在这里同步补断言即可。
 */
import { describe, it, expect } from "vitest";
import { zhCN, enUS } from "../i18n";

type Dictionary = Record<string, unknown>;

/** 与 `frontend/src/types/kpiCatalog.ts` 的 `KpiDatasourceSuggestionReason` 一一对应。 */
const REASONS = [
  "legacy_name_exact",
  "legacy_name_fuzzy",
  "legacy_table_exact",
  "ontology_table",
] as const;

const DICTIONARIES: [string, Dictionary][] = [
  ["zh-CN", zhCN as unknown as Dictionary],
  ["en-US", enUS as unknown as Dictionary],
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

describe("KPI 数据源建议 — reason i18n", () => {
  it.each(DICTIONARIES)("%s 词典含全部 reason 且译文非空", (_locale, dict) => {
    for (const reason of REASONS) {
      expectNonEmpty(dict, ["kpiCatalog", "suggestions", "reasons", reason]);
    }
  });

  it("zh-CN 与 en-US 的 reason 集合一致", () => {
    const zhReasons = lookup(zhCN as unknown as Dictionary, [
      "kpiCatalog",
      "suggestions",
      "reasons",
    ]);
    const enReasons = lookup(enUS as unknown as Dictionary, [
      "kpiCatalog",
      "suggestions",
      "reasons",
    ]);

    expect(Object.keys(zhReasons as object).sort()).toEqual(
      Object.keys(enReasons as object).sort()
    );
  });

  it("legacy_table_exact 在中英两侧都存在（后端新增的裸表名精确 join 信号）", () => {
    expect(zhCN.kpiCatalog.suggestions.reasons).toHaveProperty("legacy_table_exact");
    expect(enUS.kpiCatalog.suggestions.reasons).toHaveProperty("legacy_table_exact");
  });
});
