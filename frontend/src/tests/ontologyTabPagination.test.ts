// 守卫：本体管理页的**五个** Tab 必须统一用仓库标准分页（useTablePagination）。
//
// 为什么需要它（2026-11-17）：
//   本体管理页的「指标」「语义关系」两个 Tab 当时没有自己的分页配置，
//   只吃 antd Table 的**默认分页**（pageSize=10、无总条数、无每页条数选择、无快速跳页）。
//   行数少的 Tab（指标、语义关系）因此「看不到分页」；行数多的 Tab（类、属性、关联）
//   又与本仓库其它页面（本体属性管理 / KPI 目录 / 数据域）的分页体验不一致。
//   ⇒ 统一到 `useTablePagination`（pageSize=20 + 10/20/50/100 + 快速跳页 + 总条数）。
//
// 为什么用源码级扫描而不是渲染断言：
//   五个 Tab 的数据源各不相同（类/属性/指标/关联/语义关系），逐个渲染 + 造 25 条数据
//   会让本文件变成一整套集成测试；而「有没有把标准分页接到 Table 上」这件事
//   是**结构性**的，源码扫描一眼就能看全，且不会因为 mock 数据造假绿。
//
// 判定口径（不多不少）：
//   每个 Tab 组件源码里必须同时出现
//     1) `useTablePagination`（从 utils/useTablePagination 引入并调用）
//     2) `pagination={pagination}`（把 hook 返回的 pagination 接到主表格上）
//   注：ClassTab 的「版本历史」弹窗内表格显式 `pagination={false}`，那是
//   另一个语义（弹窗内不翻页），不影响本守卫。

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";

/** 本文件位于 src/tests/ ⇒ 上一级就是 src/。用 import.meta.dirname 定位（不依赖 cwd）。 */
const HERE = (import.meta as unknown as { dirname?: string }).dirname ?? process.cwd();
const COMPONENT_DIR = existsSync(join(HERE, "..", "components", "ontology"))
  ? resolve(HERE, "..", "components", "ontology")
  : resolve(process.cwd(), "src", "components", "ontology");

/** 本体管理页的五个 Tab（顺序 = 页面上的标签页顺序）。 */
const TAB_COMPONENTS = [
  "ClassTab",
  "PropertyTab",
  "MetricTab",
  "JoinTab",
  "SemanticRelationTab",
] as const;

describe("守卫：本体管理五个 Tab 统一使用标准分页", () => {
  it("五个 Tab 组件都在（防空扫描假绿）", () => {
    expect(TAB_COMPONENTS.length).toBe(5);
    for (const name of TAB_COMPONENTS) {
      expect(existsSync(resolve(COMPONENT_DIR, `${name}.tsx`))).toBe(true);
    }
  });

  it.each(TAB_COMPONENTS)("%s 使用 useTablePagination 并把 pagination 接到表格上", (name) => {
    const src = readFileSync(resolve(COMPONENT_DIR, `${name}.tsx`), "utf-8");

    expect(src, `${name} 未引入 useTablePagination`).toContain("useTablePagination");
    expect(src, `${name} 未把 pagination 传给 Table`).toMatch(/pagination=\{pagination\}/);
  });
});
