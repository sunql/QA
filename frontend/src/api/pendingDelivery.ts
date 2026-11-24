/** 待到货明细页 API（欠交明细 · 服务端分页）。
 *
 * 与 Copilot 场景内的工具口径一致（同 SQL builder），但独立端点：
 * 工具 limit 上限 50；明细页允许更多行并分页。
 *
 * 多值参数用**逗号分隔**（axios 默认把数组序列化成 `key[]=`，与 FastAPI 的
 * `list[str]` 对不上 ⇒ 静默空列表）。
 */

import { httpClient } from "./client";

export interface PendingDeliveryLine {
  poNo: string;
  poLine: number;
  materialCode: string;
  materialDesc?: string | null;
  supplierCode: string;
  supplierName?: string | null;
  orderedQty: number;
  receivedQty: number;
  openQty: number;
  expectedDate: string | null;
  overdueDays: number;
}

export interface PendingDeliveryLinesPage {
  total: number;
  offset: number;
  limit: number;
  rows: PendingDeliveryLine[];
}

export interface PendingDeliveryLinesQuery {
  datasourceId?: number;
  monthsBack?: number;
  dateFrom?: string;
  dateTo?: string;
  supplierKeys?: string[];
  materialKeys?: string[];
  siteKeys?: string[];
  minOverdueDays?: number;
  offset?: number;
  limit?: number;
}

function toParams(query: PendingDeliveryLinesQuery): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  if (query.datasourceId !== undefined) out.datasourceId = query.datasourceId;
  if (query.monthsBack !== undefined) out.monthsBack = query.monthsBack;
  if (query.dateFrom) out.dateFrom = query.dateFrom;
  if (query.dateTo) out.dateTo = query.dateTo;
  if (query.supplierKeys?.length) out.supplierKeys = query.supplierKeys.join(",");
  if (query.materialKeys?.length) out.materialKeys = query.materialKeys.join(",");
  if (query.siteKeys?.length) out.siteKeys = query.siteKeys.join(",");
  if (query.minOverdueDays !== undefined) out.minOverdueDays = query.minOverdueDays;
  if (query.offset !== undefined) out.offset = query.offset;
  if (query.limit !== undefined) out.limit = query.limit;
  return out;
}

/** 欠交明细分页查询。 */
export async function listPendingDeliveryLines(
  query: PendingDeliveryLinesQuery = {},
): Promise<PendingDeliveryLinesPage> {
  const res = await httpClient.get<PendingDeliveryLinesPage>(
    "/procurement/pending-delivery/lines",
    { params: toParams(query) },
  );
  return res.data;
}
