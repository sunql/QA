import type { MeasureSpec } from "../types/ontology";

/** 分子/分母在表单里的扁平形状（三个输入框，不嵌套对象）。 */
export interface MeasureFormValues {
  agg: string | undefined;
  // 字段未注册时（如 MEASURE 类型下的分母）antd 不返回值 ⇒ 运行时是 undefined。
  expr: string | undefined;
  filter: string | undefined;
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
  const expr = values.expr?.trim() ?? "";
  const filter = values.filter?.trim() ?? "";
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

/** 需要分母的指标类型（比率 / 差值），与后端 kpi_metric_reconcile 一致。 */
const TYPES_REQUIRING_DENOMINATOR = new Set(["RATIO", "DIFFERENCE"]);

/** 该指标类型是否需要在表单里暴露分母行。 */
export function needsDenominatorMeasure(metricType: string | undefined): boolean {
  return TYPES_REQUIRING_DENOMINATOR.has(metricType ?? "");
}

/** 分母的表单取值 + 指标类型（决定表单有没有暴露分母行）。 */
export interface DenominatorMeasureInput extends MeasureFormValues {
  /** 表单里当前选中的指标类型。 */
  metricType: string | undefined;
  /**
   * 库里**原来**的指标类型（编辑场景；新建时为 undefined）。
   *
   * 需要它来区分两种「表单没暴露分母行」：
   * - 原来是 MEASURE ⇒ 这一行从来没渲染过 ⇒ 用户没碰过 ⇒ 原样回传（不破坏）；
   * - 原来是 RATIO/DIFFERENCE、这次改成了别的类型 ⇒ **改型本身就是「不要分母」的意图**
   *   ⇒ 显式清空（与修复前的行为一致）。
   */
  originalMetricType?: string | undefined;
}

/**
 * 原样回传库里的 measure，但**只保留后端 schema 允许的 5 个键**，并返回**新对象**。
 *
 * 为什么裁剪：后端 `MeasureSpecSchema` 是 `extra="forbid"` ⇒ JSONB 里若带了
 * schema 之外的键，回传时整个请求会被 422 拒掉 —— 比「静默清空」更糟（用户连改名都存不了）。
 * 为什么复制：下游若原地改 payload，不该连带改到从接口拿到的指标对象。
 */
export function sanitizeMeasureSpec(
  spec: MeasureSpec | null | undefined,
): MeasureSpec | null {
  if (!spec) return null;
  const raw = spec as unknown as Record<string, unknown>;
  return {
    agg: typeof raw.agg === "string" ? raw.agg : "",
    expr: typeof raw.expr === "string" ? raw.expr : null,
    filter: typeof raw.filter === "string" ? raw.filter : null,
    class_id: typeof raw.class_id === "number" ? raw.class_id : null,
    class_alias: typeof raw.class_alias === "string" ? raw.class_alias : null,
  };
}

/**
 * 分母 measure 的取值规则（分子不需要这套：分子行**总是**渲染）。
 *
 * 表单只对 RATIO / DIFFERENCE 渲染分母行；其余类型（MEASURE 等）下分母字段
 * **从未注册到 antd 表单** ⇒ `validateFields` 不返回它们 ⇒ 表单值是 `undefined`。
 * 若把 `undefined` 当成「用户清空」提交 `null`，后端（`ontology_service.updateMetric`
 * 整块替换 JSONB）就会把库里的历史分母连同 `class_id` / `class_alias` 一起抹掉 ——
 * 用户从没见过这个字段，却因为点了一次「保存」而丢数据。
 *
 * 三条规则（按顺序）：
 * 1. 表单**暴露**了分母行（当前类型是 RATIO / DIFFERENCE）⇒ 表单值说了算
 *    （用户显式清空 ⇒ `null`，是合法意图）；
 * 2. 当前类型不需要分母、但**原类型**需要（本次把比率/差值改成了别的类型）
 *    ⇒ 改型本身就是「不要分母」的意图 ⇒ 显式清空（与修复前行为一致）；
 * 3. 原类型也不需要分母 ⇒ 这一行**从来没渲染过** ⇒ 用户没碰过 ⇒ 原样回传
 *    （不破坏；经 `sanitizeMeasureSpec` 裁剪到后端 schema 允许的 5 个键并返回新对象）。
 */
export function resolveDenominatorMeasure(
  values: DenominatorMeasureInput,
  existing: MeasureSpec | null | undefined,
): MeasureSpec | null {
  if (needsDenominatorMeasure(values.metricType)) {
    return toMeasureSpec(values, existing);
  }
  if (needsDenominatorMeasure(values.originalMetricType)) {
    return null;
  }
  return sanitizeMeasureSpec(existing);
}
