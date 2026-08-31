import { httpClient } from "./client";
import type {
  FeatureComputeBatchResult,
  FeatureComputeResult,
  FeatureDefinition,
  FeatureDefinitionCreate,
  FeatureDefinitionUpdate,
  FeatureValue,
} from "../types/feature";

const BASE = "/features";

export async function listFeatures(): Promise<FeatureDefinition[]> {
  const res = await httpClient.get<FeatureDefinition[]>(BASE);
  return res.data;
}

export async function getFeature(id: number): Promise<FeatureDefinition> {
  const res = await httpClient.get<FeatureDefinition>(`${BASE}/${id}`);
  return res.data;
}

export async function createFeature(
  payload: FeatureDefinitionCreate,
): Promise<FeatureDefinition> {
  const res = await httpClient.post<FeatureDefinition>(BASE, payload);
  return res.data;
}

export async function updateFeature(
  id: number,
  payload: FeatureDefinitionUpdate,
): Promise<FeatureDefinition> {
  const res = await httpClient.put<FeatureDefinition>(`${BASE}/${id}`, payload);
  return res.data;
}

export async function deleteFeature(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/${id}`);
}

export async function computeFeature(id: number): Promise<FeatureComputeResult> {
  const res = await httpClient.post<FeatureComputeResult>(`${BASE}/${id}/compute`);
  return res.data;
}

export async function computeAllFeatures(): Promise<FeatureComputeBatchResult> {
  const res = await httpClient.post<FeatureComputeBatchResult>(`${BASE}/compute-batch`);
  return res.data;
}

export async function listFeatureValues(id: number): Promise<FeatureValue[]> {
  const res = await httpClient.get<FeatureValue[]>(`${BASE}/${id}/values`);
  return res.data;
}
