// Embedding 服务类型
export type EmbeddingProviderType = "ollama" | "omlx" | "openai_compatible";

// Embedding 服务（读取，与后端 EmbeddingProviderRead camelCase 对齐；不含 apiKey）
export interface EmbeddingProvider {
  id: number;
  name: string;
  providerType: EmbeddingProviderType;
  baseUrl: string;
  modelName: string;
  dimension: number;
  isActive: boolean;
  createdTime: string;
  updatedTime: string;
}

// 创建（apiKey 明文输入，后端 Fernet 加密存储）
export interface EmbeddingProviderCreate {
  name: string;
  providerType: EmbeddingProviderType;
  baseUrl: string;
  modelName: string;
  apiKey?: string;
  dimension?: number;
  isActive?: boolean;
}

// 更新（部分字段；apiKey 置 null 清除，省略表示不修改）
export interface EmbeddingProviderUpdate {
  name?: string;
  providerType?: EmbeddingProviderType;
  baseUrl?: string;
  modelName?: string;
  apiKey?: string | null;
  dimension?: number;
  isActive?: boolean;
}

// 类型下拉选项（provider_type 为技术名词，label 用原文不翻译）
export const PROVIDER_TYPE_OPTIONS: { value: EmbeddingProviderType; label: string }[] = [
  { value: "ollama", label: "Ollama" },
  { value: "omlx", label: "oMLX" },
  { value: "openai_compatible", label: "OpenAI Compatible" },
];
