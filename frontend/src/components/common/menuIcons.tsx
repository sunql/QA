import { createElement } from "react";
import type { ComponentType, ReactNode } from "react";
import {
  RobotOutlined, MessageOutlined, ThunderboltOutlined, AppstoreOutlined,
  BarChartOutlined, AlertOutlined, PartitionOutlined, DatabaseOutlined,
  AuditOutlined, FileTextOutlined, DashboardOutlined, NodeIndexOutlined,
  ApartmentOutlined, CodeOutlined, FundProjectionScreenOutlined,
  NumberOutlined, SettingOutlined, ApiOutlined, HeartOutlined,
  SafetyCertificateOutlined, ClusterOutlined, TagsOutlined, ToolOutlined,
  UserOutlined, TeamOutlined, UsergroupAddOutlined, MenuOutlined, ImportOutlined,
  BookOutlined,
} from "@ant-design/icons";

// Ant Design icon exports are ForwardRefExoticComponent objects, not plain functions.
// Wrap each in a thin function component so registry entries are plain functions.
const wrap = (Icon: ComponentType): ComponentType => {
  const Wrapped: ComponentType = () => createElement(Icon);
  return Wrapped;
};

export const ICON_REGISTRY: Record<string, ComponentType> = {
  robot: wrap(RobotOutlined), message: wrap(MessageOutlined), thunderbolt: wrap(ThunderboltOutlined),
  appstore: wrap(AppstoreOutlined), barchart: wrap(BarChartOutlined), alert: wrap(AlertOutlined),
  partition: wrap(PartitionOutlined), database: wrap(DatabaseOutlined), audit: wrap(AuditOutlined),
  file: wrap(FileTextOutlined), dashboard: wrap(DashboardOutlined), node: wrap(NodeIndexOutlined),
  apartment: wrap(ApartmentOutlined), code: wrap(CodeOutlined), fund: wrap(FundProjectionScreenOutlined),
  number: wrap(NumberOutlined), setting: wrap(SettingOutlined), api: wrap(ApiOutlined),
  heart: wrap(HeartOutlined), safety: wrap(SafetyCertificateOutlined), cluster: wrap(ClusterOutlined),
  tags: wrap(TagsOutlined), tool: wrap(ToolOutlined),
  user: wrap(UserOutlined), team: wrap(TeamOutlined), org: wrap(UsergroupAddOutlined),
  menu: wrap(MenuOutlined), import: wrap(ImportOutlined),
  book: wrap(BookOutlined),
};

export const renderIcon = (code?: string): ReactNode => {
  if (!code) return null;
  const Icon = ICON_REGISTRY[code];
  return Icon ? createElement(Icon) : null;
};
