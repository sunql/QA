// 业务数据库类型
export type DataSourceType = "mysql" | "postgresql" | "oracle";

// Oracle 版本：决定分页语法（11g 用 ROWNUM，12c+ 用 FETCH FIRST）
export type OracleVersion = "11g" | "12c+";

// 数据源（读取，不含密码）
export interface DataSource {
  id: number;
  name: string;
  type: DataSourceType;
  host: string;
  port: number;
  databaseName: string;
  username: string;
  description: string | null;
  isActive: boolean;
  isDefault: boolean;
  // 后端存储任意版本字符串（如 "11g"/"12c"/"19c"），前端表单仅提供 11g / 12c+ 两选项
  oracleVersion: string | null;
  createdBy: string | null;
  createdTime: string;
  updatedTime: string;
}

// 创建数据源（密码明文输入，后端加密存储）
export interface DataSourceCreate {
  name: string;
  type: DataSourceType;
  host: string;
  port: number;
  databaseName: string;
  username: string;
  password: string;
  description?: string | null;
  isActive?: boolean;
  isDefault?: boolean;
  oracleVersion?: string | null;
}

// 更新数据源（部分字段，password 可选）
export interface DataSourceUpdate {
  name?: string;
  type?: DataSourceType;
  host?: string;
  port?: number;
  databaseName?: string;
  username?: string;
  password?: string;
  description?: string | null;
  isActive?: boolean;
  isDefault?: boolean;
  oracleVersion?: string | null;
}

// 连接测试请求
export interface DataSourceTestRequest {
  type: DataSourceType;
  host: string;
  port: number;
  databaseName: string;
  username: string;
  password: string;
}

// 连接测试响应
export interface DataSourceTestResponse {
  success: boolean;
  message: string;
}

// 类型选项（用于下拉）—— labelKey 在组件渲染时通过 t() 解析
export const DATASOURCE_TYPE_OPTIONS: { value: DataSourceType; labelKey: "oracle" | "postgresql" | "mysql" }[] = [
  { value: "oracle", labelKey: "oracle" },
  { value: "postgresql", labelKey: "postgresql" },
  { value: "mysql", labelKey: "mysql" },
];

// 各类型默认端口（切换类型时自动填充）
export const DATASOURCE_DEFAULT_PORTS: Record<DataSourceType, number> = {
  oracle: 1521,
  postgresql: 5432,
  mysql: 3306,
};

// Oracle 版本选项（11g 用 ROWNUM 分页；12c 及以上用 FETCH FIRST）
export const ORACLE_VERSION_OPTIONS: { value: OracleVersion; labelKey: "modern" | "legacy" }[] = [
  { value: "12c+", labelKey: "modern" },
  { value: "11g", labelKey: "legacy" },
];

// ===== schema 发现（introspection）=====
// 与后端 SchemaIntrospectResponse / TableSchemaRead / ColumnSchemaRead 的
// camelCase JSON 契约一致（见 backend/app/domain/schemas.py）。

export interface ColumnSchema {
  columnName: string;
  dataType: string;
  nullable: boolean;
}

export interface ForeignKeySchema {
  columnName: string;
  refTable: string;
  refColumn: string;
}

export interface TableSchema {
  tableName: string;
  owner: string;
  columns: ColumnSchema[];
  primaryKeys: string[];
  foreignKeys: ForeignKeySchema[];
}

export interface SchemaIntrospectResponse {
  tables: TableSchema[];
  cachedAt: string;
}
