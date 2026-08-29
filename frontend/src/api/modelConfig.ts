import { httpClient } from "./client";
import type { ModelConfig, ModelConfigCreate, ModelConfigUpdate } from "../types/modelConfig";

const BASE = "/models";

export async function listModels(activeOnly = false): Promise<ModelConfig[]> {
  const res = await httpClient.get<ModelConfig[]>(BASE, {
    params: { active_only: activeOnly },
  });
  return res.data;
}

export async function getModel(id: number): Promise<ModelConfig> {
  const res = await httpClient.get<ModelConfig>(`${BASE}/${id}`);
  return res.data;
}

export async function createModel(payload: ModelConfigCreate): Promise<ModelConfig> {
  const res = await httpClient.post<ModelConfig>(BASE, payload);
  return res.data;
}

export async function updateModel(
  id: number,
  payload: ModelConfigUpdate
): Promise<ModelConfig> {
  const res = await httpClient.put<ModelConfig>(`${BASE}/${id}`, payload);
  return res.data;
}

export async function deactivateModel(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/${id}`);
}
