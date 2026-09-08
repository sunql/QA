import { httpClient } from "./client";
import type { ModelConfig, ModelConfigCreate, ModelConfigUpdate } from "../types/modelConfig";

const BASE = "/models";

export async function listModels(activeOnly = false): Promise<ModelConfig[]> {
  // 关键：query 参数名是 camelCase（与 FastAPI kwarg 名一致），
  // 后端 router 是 `activeOnly: bool = Query(...)`，写 snake_case
  // 会被静默忽略，导致 activeOnly=false（默认），返回所有模型（含已停用）。
  const res = await httpClient.get<ModelConfig[]>(BASE, {
    params: { activeOnly },
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
