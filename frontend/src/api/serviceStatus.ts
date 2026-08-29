import { httpClient } from "./client";
import type { ServiceStatusResponse } from "../types/serviceStatus";

const BASE = "/system";

export async function getServiceStatus(): Promise<ServiceStatusResponse> {
  const res = await httpClient.get<ServiceStatusResponse>(`${BASE}/status`);
  return res.data;
}
