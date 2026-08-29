import { httpClient } from "./client";
import type {
  EmbeddingProvider,
  EmbeddingProviderCreate,
  EmbeddingProviderUpdate,
} from "../types/embeddingProvider";

const BASE = "/embedding-providers";

export async function listEmbeddingProviders(): Promise<EmbeddingProvider[]> {
  const res = await httpClient.get<EmbeddingProvider[]>(BASE);
  return res.data;
}

export async function createEmbeddingProvider(
  payload: EmbeddingProviderCreate
): Promise<EmbeddingProvider> {
  const res = await httpClient.post<EmbeddingProvider>(BASE, payload);
  return res.data;
}

export async function updateEmbeddingProvider(
  id: number,
  payload: EmbeddingProviderUpdate
): Promise<EmbeddingProvider> {
  const res = await httpClient.put<EmbeddingProvider>(`${BASE}/${id}`, payload);
  return res.data;
}

export async function activateEmbeddingProvider(id: number): Promise<EmbeddingProvider> {
  const res = await httpClient.post<EmbeddingProvider>(`${BASE}/${id}/activate`);
  return res.data;
}

export async function deactivateEmbeddingProvider(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/${id}`);
}
