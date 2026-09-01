import type { MenuConfig } from "../types/menuConfig";

const API_BASE = "/api/v1";

export async function fetchMenuConfig(): Promise<MenuConfig> {
  const resp = await fetch(`${API_BASE}/menu-config`, {
    headers: { Accept: "application/json" },
    credentials: "include",
  });
  if (!resp.ok) {
    throw new Error(`fetchMenuConfig failed: ${resp.status} ${resp.statusText}`);
  }
  return (await resp.json()) as MenuConfig;
}
