// 模型供应商类型（统一用 DB 层小写下划线格式存储，与后端 ProviderType 枚举值对齐）
export type ProviderType =
  | "openai"
  | "azure_openai"
  | "moonshot"
  | "openai_compatible_proxy"
  | "ollama";

// 模型配置（读取）
export interface ModelConfig {
  id: number;
  modelName: string;
  provider: ProviderType;
  apiEndpoint: string;
  isActive: boolean;
  costPer1KInput: number | string;
  costPer1KOutput: number | string;
  maxInputTokens: number;
  weight: number;
  costThreshold: number | string;
  temperature?: number;
  createdAt: string;
  updatedAt: string;
}

// 创建模型配置（API Key 明文输入，后端加密存储）
export interface ModelConfigCreate {
  modelName: string;
  provider: ProviderType;
  apiEndpoint: string;
  apiKey: string;
  costPer1KInput: number | string;
  costPer1KOutput: number | string;
  maxInputTokens: number;
  weight: number;
  costThreshold: number | string;
  temperature?: number;
}

// 更新模型配置（部分字段，apiKey 可选）
export interface ModelConfigUpdate {
  modelName?: string;
  provider?: ProviderType;
  apiEndpoint?: string;
  apiKey?: string;
  costPer1KInput?: number;
  costPer1KOutput?: number;
  maxInputTokens?: number;
  weight?: number;
  costThreshold?: number;
  isActive?: boolean;
  temperature?: number;
}

// 供应商选项（用于下拉）—— value 与 DB 层小写下划线格式一致，labelKey 用于 i18n t() 解析
export const PROVIDER_OPTIONS: { value: ProviderType; labelKey: string }[] = [
  { value: "openai", labelKey: "enums.provider.openai" },
  { value: "azure_openai", labelKey: "enums.provider.azure_openai" },
  { value: "moonshot", labelKey: "enums.provider.moonshot" },
  { value: "openai_compatible_proxy", labelKey: "enums.provider.openai_compatible_proxy" },
  { value: "ollama", labelKey: "enums.provider.ollama" },
];
