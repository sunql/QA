import type { MeasureSpec } from "../types/ontology";

/** 分子/分母在表单里的扁平形状（三个输入框，不嵌套对象）。 */
export interface MeasureFormValues {
  agg: string | undefined;
  expr: string;
  filter: string;
}

export const EMPTY_MEASURE_FORM: MeasureFormValues = {
  agg: undefined,
  expr: "",
  filter: "",
};

/**
 * 扁平表单 → measure JSON（`null` = 显式清空）。
 *
 * 返回 `null` 而不是 `undefined`：后端 `updateMetric` 用
 * `model_dump(exclude_unset=True)`，axios 会丢弃值为 `undefined` 的键 ⇒
 * 清空的 measure 根本不会出现在请求体里 ⇒ 后端静默保留旧值。
 * 显式传 `null` 才能真的清空（Create/Update 两边都允许 `None`）。
 *
 * 缺 agg ⇒ null；既无 expr 又无 filter ⇒ null（空 measure 等于没有 measure）。
 *
 * `existing`：编辑时传入库里原有的 measure ⇒ 把 `class_id` / `class_alias`
 * 原样带回。后端更新是**整块替换 JSONB**，不回传就会抹掉回填脚本（Task 15）
 * 写入的源类信息。这两个键的权威是回填脚本，前端只负责「不破坏」，不负责推导。
 */
export function toMeasureSpec(
  values: MeasureFormValues,
  existing?: MeasureSpec | null,
): MeasureSpec | null {
  const agg = (values.agg ?? "").trim().toUpperCase();
  if (!agg) return null;
  const expr = values.expr.trim();
  const filter = values.filter.trim();
  if (!expr && !filter) return null;
  return {
    agg,
    expr: expr || null,
    filter: filter || null,
    class_id: existing?.class_id ?? null,
    class_alias: existing?.class_alias ?? null,
  };
}

/** measure JSON → 扁平表单（容错：非对象 / 缺字段 ⇒ 空表单）。 */
export function fromMeasureSpec(spec: unknown): MeasureFormValues {
  if (!spec || typeof spec !== "object") return { ...EMPTY_MEASURE_FORM };
  const raw = spec as Partial<MeasureSpec>;
  return {
    agg: typeof raw.agg === "string" ? raw.agg.toUpperCase() : undefined,
    expr: typeof raw.expr === "string" ? raw.expr : "",
    filter: typeof raw.filter === "string" ? raw.filter : "",
  };
}
