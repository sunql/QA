// 模型供应商类型
export type ProviderType =
  | "OPENAI"
  | "AZURE_OPENAI"
  | "OPENAI_COMPATIBLE_PROXY"
  | "OLLAMA";

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
}

// 供应商选项（用于下拉）—— labelKey 在组件渲染时通过 t() 解析
export const PROVIDER_OPTIONS: { value: ProviderType; labelKey: ProviderType }[] = [
  { value: "OPENAI", labelKey: "OPENAI" },
  { value: "AZURE_OPENAI", labelKey: "AZURE_OPENAI" },
  { value: "OPENAI_COMPATIBLE_PROXY", labelKey: "OPENAI_COMPATIBLE_PROXY" },
  { value: "OLLAMA", labelKey: "OLLAMA" },
];
