/** 主题 token 定义。
 *
 * 设计原则：
 * - 单色源（single source of truth）：同一种语义颜色既喂给 antd theme.token，
 *   也喂给 cssVariables 模块，避免双轨不一致。
 * - 颜色取自 uidemo/ 截图风格：暗色工业控制台，主色 teal #00D9C0。
 * - 形状全局硬朗化：borderRadius 2 / borderRadiusLG 4，阴影减弱（详见 ThemedRoot components 配置）。
 *
 * 用法：
 *   const token = isDark ? DARK_TOKEN : LIGHT_TOKEN;
 *   <ConfigProvider theme={{ token, algorithm }} />
 *   applyCssVarsToRoot(token);
 */

/** 血缘层级色板（Phase D：ECharts token 化）。
 * 7 层 SOURCE_SYSTEM → AI 各自一色，与 LineageGraph.LAYER_COLORS 对齐。
 * 暗色下保持原 antd 调色板（高对比度），亮色下稍降饱和以避免太刺眼。
 */
export interface LayerPalette {
  layerSourceSystem: string;
  layerOds: string;
  layerDwd: string;
  layerDws: string;
  layerAds: string;
  layerKpi: string;
  layerAi: string;
}

/** 主题 token 数据契约。所有颜色用 HEX 字符串，圆角用 px 数字。 */
export interface ThemeToken {
  // 主色与品牌色
  colorPrimary: string;
  // 背景层级
  colorBgBase: string;       // 页面底色
  colorBgContainer: string;  // 卡片/容器色
  // 边框
  colorBorder: string;
  // 语义色
  colorSuccess: string;
  colorWarning: string;
  colorError: string;
  // 文字三档
  colorText: string;             // 主文字
  colorTextSecondary: string;    // 次文字
  colorTextTertiary: string;     // 辅助文字
  // 形状
  borderRadius: number;    // 全局基础圆角（按钮/输入/卡片）
  borderRadiusLG: number;  // 较大圆角（大卡片/容器）
  // 血缘层级色板（Phase D）
  layers: LayerPalette;
}

/** 暗色 token（默认主题）。工业控制台风格，深海军蓝底 + teal 主色。 */
export const DARK_TOKEN: ThemeToken = {
  colorPrimary: "#00D9C0",
  colorBgBase: "#0f1e2e",
  colorBgContainer: "#152838",
  colorBorder: "#1f3a52",
  colorSuccess: "#00D9C0",
  colorWarning: "#FF8C42",
  colorError: "#F56C6C",
  colorText: "#FFFFFF",
  colorTextSecondary: "#cbd5e1",
  colorTextTertiary: "#94a3b8",
  borderRadius: 2,
  borderRadiusLG: 4,
  layers: {
    layerSourceSystem: "#1677ff",
    layerOds: "#13c2c2",
    layerDwd: "#52c41a",
    layerDws: "#722ed1",
    layerAds: "#fa8c16",
    layerKpi: "#f5222d",
    layerAi: "#eb2f96",
  },
};

/** 亮色 token（备选主题）。白底 + 深 teal 主色，对比度满足 WCAG AA。 */
export const LIGHT_TOKEN: ThemeToken = {
  colorPrimary: "#00B8A9",  // teal 深一档，亮色背景上对比度更好
  colorBgBase: "#f5f7fa",
  colorBgContainer: "#ffffff",
  colorBorder: "#e4e7ed",
  colorSuccess: "#00B8A9",
  colorWarning: "#FA8C16",
  colorError: "#F5222D",
  colorText: "#1f2933",
  colorTextSecondary: "#4b5563",
  colorTextTertiary: "#8c8c8c",
  borderRadius: 2,
  borderRadiusLG: 4,
  layers: {
    layerSourceSystem: "#1677ff",
    layerOds: "#13c2c2",
    layerDwd: "#52c41a",
    layerDws: "#722ed1",
    layerAds: "#fa8c16",
    layerKpi: "#f5222d",
    layerAi: "#eb2f96",
  },
};