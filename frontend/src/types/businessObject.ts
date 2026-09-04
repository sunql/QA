// BusinessObject types (Phase 4.4)
// snake_case DB fields are camelCased by backend CamelModel alias_generator

export type BusinessObjectCode =
  | "SUPPLIER"
  | "MATERIAL"
  | "PO"
  | "GR"
  | "IQC"
  | "NCR";

export const BUSINESS_OBJECT_OPTIONS: BusinessObjectCode[] = [
  "SUPPLIER",
  "MATERIAL",
  "PO",
  "GR",
  "IQC",
  "NCR",
];

export interface BusinessObjectBase {
  name: string;
  headerClassId?: number | null;
  graphLabel?: string | null;
  description?: string | null;
}

export interface BusinessObjectCreate extends BusinessObjectBase {
  code: BusinessObjectCode;
}

export interface BusinessObjectUpdate {
  name?: string;
  headerClassId?: number | null;
  graphLabel?: string | null;
  description?: string | null;
}

export interface BusinessObjectRead extends BusinessObjectCreate {
  /** ISO datetime string; null when DTO is used in unpersisted construction */
  createdTime?: string;
  /** ISO datetime string; null when DTO is used in unpersisted construction */
  updatedTime?: string;
}
