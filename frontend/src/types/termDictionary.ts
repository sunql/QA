/** NL2SQL 术语字典类型 — 对应后端 TermDictionaryCreate/Read（camelCase 契约）。 */

export interface TermDictionary {
  id: number;
  term: string;
  definition: string;
  mappedClassName: string | null;
  mappedPropertyName: string | null;
  formulaHint: string | null;
  createdBy: string | null;
  createdTime: string | null;
  updatedTime: string | null;
}

export interface TermDictionaryCreate {
  term: string;
  definition: string;
  mappedClassName?: string | null;
  mappedPropertyName?: string | null;
  formulaHint?: string | null;
}
