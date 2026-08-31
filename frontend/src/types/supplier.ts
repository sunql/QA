// Supplier 360° ADS 视图（Phase 5.3）
// 对齐后端 Supplier360Profile / Supplier360EntityCode / Supplier360Kpi / Supplier360Read。
// 单供应商主数据 + 跨系统编码 + 关键 Feature 聚合（OTD/质量/价格/风险）。
// 全部 snake_case → camelCase 由后端 Pydantic alias_generator 完成，前端仅消费 camelCase。

export interface Supplier360Profile {
  enterpriseKey: number;
  enterpriseCode: string;
  owner: string | null;
  matchRule: string | null;
  effectiveDate: string | null;
  expiryDate: string | null;
}

export interface Supplier360EntityCode {
  sourceSystem: string;
  sourceKey: string;
  sourceCode: string;
  matchRule: string | null;
  effectiveDate: string | null;
  expiryDate: string | null;
}

export interface Supplier360Kpi {
  featureName: string;
  featureAlias: string | null;
  unit: string | null;
  windowSize: string | null;
  // 后端 Decimal → string（保留精度）
  value: string | null;
  // latest=true 表示 feature_definition 启用 + 有最新 value；false 表示 feature
  // 定义存在但被禁用 / DRAFT / 无 value（前端用 placeholder 渲染）。
  latest: boolean;
  validAt: string | null;
  computedAt: string | null;
}

export interface Supplier360Read {
  profile: Supplier360Profile;
  entityCodes: Supplier360EntityCode[];
  kpis: Supplier360Kpi[];
  fetchedAt: string;
}