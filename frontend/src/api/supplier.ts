import { httpClient } from "./client";
import type { Supplier360Read } from "../types/supplier";

const BASE = "/supplier-360";

/** 单供应商 360° ADS 视图查询（直接 API，跳过 Chat NL2SQL 链路）。 */
export async function getSupplier360(supplierKey: number): Promise<Supplier360Read> {
  const res = await httpClient.get<Supplier360Read>(`${BASE}/${supplierKey}`);
  return res.data;
}