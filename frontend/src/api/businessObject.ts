import { httpClient } from "./client";
import type {
  BusinessObject,
  BusinessObjectCreate,
  BusinessObjectUpdate,
} from "../types/businessObject";

const BASE = "/business-objects";

export async function listBusinessObjects(): Promise<BusinessObject[]> {
  const res = await httpClient.get<BusinessObject[]>(BASE);
  return res.data;
}

export async function getBusinessObject(
  code: string,
): Promise<BusinessObject> {
  const res = await httpClient.get<BusinessObject>(`${BASE}/${code}`);
  return res.data;
}

export async function createBusinessObject(
  payload: BusinessObjectCreate,
): Promise<BusinessObject> {
  const res = await httpClient.post<BusinessObject>(BASE, payload);
  return res.data;
}

export async function updateBusinessObject(
  code: string,
  payload: BusinessObjectUpdate,
): Promise<BusinessObject> {
  const res = await httpClient.put<BusinessObject>(
    `${BASE}/${code}`,
    payload,
  );
  return res.data;
}

export async function deleteBusinessObject(code: string): Promise<void> {
  await httpClient.delete(`${BASE}/${code}`);
}
