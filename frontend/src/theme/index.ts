/** 主题模块统一入口。
 *
 * 外部 import 收敛到此文件，避免散落在多处引用 ./tokens / ./cssVariables 内部细节。
 * 例：import { DARK_TOKEN, LIGHT_TOKEN, applyCssVarsToRoot } from "@/theme";
 */
export type { ThemeToken } from "./tokens";
export { DARK_TOKEN, LIGHT_TOKEN } from "./tokens";
export {
  tokensToCssVars,
  applyCssVarsToRoot,
  removeCssVarsFromRoot,
} from "./cssVariables";