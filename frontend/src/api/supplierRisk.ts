import { httpClient } from "./client";
import type { SupplierRiskRead } from "../types/supplierRisk";

const BASE = "/supplier-risk";

/** 单供应商风险 Agent 评估（直接 API，跳过 Chat NL2SQL 链路）。 */
export async function getSupplierRisk(supplierKey: number): Promise<SupplierRiskRead> {
  const res = await httpClient.get<SupplierRiskRead>(`${BASE}/${supplierKey}`);
  return res.data;
}