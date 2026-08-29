import { httpClient } from "./client";
import type { TermDictionary, TermDictionaryCreate } from "../types/termDictionary";

const BASE = "/term-dictionary";

export async function listTerms(): Promise<TermDictionary[]> {
  const res = await httpClient.get<TermDictionary[]>(BASE);
  return res.data;
}

export async function createTerm(payload: TermDictionaryCreate): Promise<TermDictionary> {
  const res = await httpClient.post<TermDictionary>(BASE, payload);
  return res.data;
}

export async function deleteTerm(id: number): Promise<void> {
  await httpClient.delete(`${BASE}/${id}`);
}
