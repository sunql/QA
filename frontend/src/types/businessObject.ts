// BusinessObject types (Phase 4.4 + Phase 4.6 dynamic registry)
// snake_case DB fields are camelCased by backend CamelModel alias_generator

/** Business object — DB-backed, codes live in `business_object` table.
 *  Use string at the type level since codes are now dynamically configurable. */
export type BusinessObjectCode = string;

/** Read shape returned by GET /api/v1/business-objects. */
export interface BusinessObject {
  code: string;
  name: string;
  headerClassId?: number | null;
  graphLabel?: string | null;
  description?: string | null;
  createdTime: string;
  updatedTime: string;
}

/** Shape for POST /api/v1/business-objects. */
export interface BusinessObjectCreate {
  code: string;
  name: string;
  headerClassId?: number | null;
  graphLabel?: string | null;
  description?: string | null;
}

/** Shape for PUT /api/v1/business-objects/{code}. */
export interface BusinessObjectUpdate {
  name?: string;
  headerClassId?: number | null;
  graphLabel?: string | null;
  description?: string | null;
}
