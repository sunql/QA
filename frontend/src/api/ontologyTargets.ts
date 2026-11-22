/** 本体对象查询（评审「关联本体」用）：类 / 属性 / 指标的只读列表。 */
import { httpClient } from "./client";

export interface OntologyClassOption {
  id: number;
  className: string;
  classAlias: string | null;
  sourceTable: string | null;
  objectType: string | null;
}

export interface OntologyPropertyOption {
  id: number;
  classId: number;
  propertyName: string;
  propertyAlias: string | null;
}

export interface OntologyMetricOption {
  id: number;
  metricName: string;
  metricAlias: string | null;
}

export async function listOntologyClasses(): Promise<OntologyClassOption[]> {
  const res = await httpClient.get<OntologyClassOption[]>("/ontology/classes");
  return res.data;
}

export async function listOntologyProperties(): Promise<OntologyPropertyOption[]> {
  const res = await httpClient.get<OntologyPropertyOption[]>("/ontology/properties");
  return res.data;
}

export async function listOntologyMetrics(): Promise<OntologyMetricOption[]> {
  const res = await httpClient.get<OntologyMetricOption[]>("/ontology/metrics");
  return res.data;
}
