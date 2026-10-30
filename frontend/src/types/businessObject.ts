// BusinessObject types (Phase 4.4 + Phase 4.6 dynamic registry)
// feat-data-domain-layer：primaryDomainId 必填（有且只有一个主域）+ attachedDomainIds 附域
// snake_case DB fields are camelCased by backend CamelModel alias_generator

/** Business object — DB-backed, codes live in `business_object` table.
 *  Use string at the type level since codes are now dynamically configurable. */
export type BusinessObjectCode = string;

/** Read shape returned by GET /api/v1/business-objects. */
export interface BusinessObject {
  code: string;
  name: string;
  headerClassId: number;
  primaryDomainId: string;
  attachedDomainIds: string[];
  graphLabel?: string | null;
  description?: string | null;
  createdTime: string;
  updatedTime: string;
}

/** Shape for POST /api/v1/business-objects. */
export interface BusinessObjectCreate {
  code: string;
  name: string;
  headerClassId: number;
  primaryDomainId: string;
  attachedDomainIds?: string[];
  graphLabel?: string | null;
  description?: string | null;
}

/** Shape for PUT /api/v1/business-objects/{code}.
 *  0133 后 header_class_id NOT NULL：不再允许清空（不传 = 保持原值）。 */
export interface BusinessObjectUpdate {
  name?: string;
  headerClassId?: number;
  primaryDomainId?: string;
  /** 全量替换语义：不传 = 不动，传数组 = 替换整组附域 */
  attachedDomainIds?: string[];
  graphLabel?: string | null;
  description?: string | null;
}
