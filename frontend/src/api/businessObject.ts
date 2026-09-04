import { httpClient } from "./client";
import type {
  BusinessObjectCreate,
  BusinessObjectRead,
  BusinessObjectUpdate,
} from "../types/businessObject";

const BASE = "/business-objects";

export async function listBusinessObjects(): Promise<BusinessObjectRead[]> {
  const res = await httpClient.get<BusinessObjectRead[]>(BASE);
  return res.data;
}

export async function getBusinessObject(
  code: string,
): Promise<BusinessObjectRead> {
  const res = await httpClient.get<BusinessObjectRead>(`${BASE}/${code}`);
  return res.data;
}

export async function createBusinessObject(
  payload: BusinessObjectCreate,
): Promise<BusinessObjectRead> {
  const res = await httpClient.post<BusinessObjectRead>(BASE, payload);
  return res.data;
}

export async function updateBusinessObject(
  code: string,
  payload: BusinessObjectUpdate,
): Promise<BusinessObjectRead> {
  const res = await httpClient.put<BusinessObjectRead>(
    `${BASE}/${code}`,
    payload,
  );
  return res.data;
}

export async function deleteBusinessObject(code: string): Promise<void> {
  await httpClient.delete(`${BASE}/${code}`);
}
