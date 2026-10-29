/**
 * KPI 目录表单值与提交 payload 构造。
 *
 * 从 `pages/KpiCatalogPage.tsx` 抽出，理由：
 * 1. `handleSubmit` 原本要把同一批字段逐字写两遍（create / update）⇒ 超 50 行；
 * 2. 表单初值必须是**工厂函数**——模块级单例会让 4 个 tags 数组共享同一实例，
 *    rc-select 的 `mode="tags"` 存在原地处理路径 ⇒ 违反「禁止原地修改」约束。
 */

import type {
  KpiCatalogCreate,
  KpiCatalogUpdate,
  KpiStatus,
} from "../types/kpiCatalog";

/** 语义匹配阈值默认值（与后端 `KpiCatalogRead.match_threshold` 默认值一致）。 */
export const DEFAULT_MATCH_THRESHOLD = 0.75;

/** KPI 目录编辑弹窗的表单值。 */
export interface KpiFormValues {
  kpiCode: string;
  kpiName: string;
  businessDefinition: string;
  formula: string;
  numerator: string;
  denominator: string;
  grain: string;
  unit: string;
  dataSource: string;
  datasourceId?: number;
  owner: string;
  version: string;
  status: KpiStatus;
  semanticKeywords: string[];
  /** rc-input-number 清空时回传 `null`（不是 undefined）⇒ 类型必须如实反映。 */
  matchThreshold: number | null;
  grainKeys: string[];
  numeratorColumns: string[];
  denominatorColumns: string[];
  metricId?: number;
}

/**
 * 空白表单初值。**每次调用返回全新对象与全新数组**，
 * 避免 tags 类控件跨次打开共享同一数组实例。
 */
export const emptyKpiForm = (): KpiFormValues => ({
  kpiCode: "",
  kpiName: "",
  businessDefinition: "",
  formula: "",
  numerator: "",
  denominator: "",
  grain: "",
  unit: "",
  dataSource: "",
  datasourceId: undefined,
  owner: "",
  version: "v1.0",
  status: "DRAFT",
  semanticKeywords: [],
  matchThreshold: DEFAULT_MATCH_THRESHOLD,
  grainKeys: [],
  numeratorColumns: [],
  denominatorColumns: [],
  metricId: undefined,
});

/** 更新 payload：显式 `null` 清空（后端 exclude_unset 契约）。 */
export function buildKpiUpdatePayload(values: KpiFormValues): KpiCatalogUpdate {
  return {
    kpiCode: values.kpiCode,
    kpiName: values.kpiName,
    businessDefinition: values.businessDefinition || null,
    formula: values.formula || null,
    numerator: values.numerator || null,
    denominator: values.denominator || null,
    grain: values.grain || null,
    unit: values.unit || null,
    dataSource: values.dataSource || null,
    datasourceId: values.datasourceId ?? null,
    owner: values.owner || null,
    version: values.version || undefined,
    status: values.status,
    // 数组一律拷贝：payload 不得持有 form store / React state 里的实例（禁止别名共享）
    semanticKeywords: [...values.semanticKeywords],
    // 清空阈值 ⇒ 复位为默认值（后端 update 路径同样把显式 null 归一为 0.75）
    matchThreshold: values.matchThreshold ?? DEFAULT_MATCH_THRESHOLD,
    grainKeys: [...values.grainKeys],
    numeratorColumns: [...values.numeratorColumns],
    denominatorColumns: [...values.denominatorColumns],
    metricId: values.metricId ?? null,
  };
}

/** 创建 payload：空串按「未填」省略（后端 exclude_unset 契约）。 */
export function buildKpiCreatePayload(values: KpiFormValues): KpiCatalogCreate {
  return {
    kpiCode: values.kpiCode,
    kpiName: values.kpiName,
    businessDefinition: values.businessDefinition || undefined,
    formula: values.formula || undefined,
    numerator: values.numerator || undefined,
    denominator: values.denominator || undefined,
    grain: values.grain || undefined,
    unit: values.unit || undefined,
    dataSource: values.dataSource || undefined,
    datasourceId: values.datasourceId!,
    owner: values.owner || undefined,
    version: values.version || undefined,
    status: values.status,
    semanticKeywords: [...values.semanticKeywords],
    matchThreshold: values.matchThreshold ?? DEFAULT_MATCH_THRESHOLD,
    grainKeys: [...values.grainKeys],
    numeratorColumns: [...values.numeratorColumns],
    denominatorColumns: [...values.denominatorColumns],
    metricId: values.metricId ?? null,
  };
}
