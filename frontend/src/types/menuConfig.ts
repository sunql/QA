export interface MenuItem {
  code: string;
  labelKey: string;
  iconCode: string | null;
  sortOrder: number;
  permissionCode: string | null;
  roles: string[];
  path: string | null;
}

export interface MenuSection extends MenuItem {
  children: MenuItem[];
}

export interface MenuConfig {
  version: string;
  sections: MenuSection[];
}
