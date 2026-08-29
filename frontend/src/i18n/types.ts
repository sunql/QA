/** 类型推导：把嵌套对象压平成点分键字面量联合类型。
 *
 * 用法：
 *   type Key = NestedKeyOf<typeof zhCN>;  // "common.refresh" | "common.edit" | ...
 *
 * 这样 ``t(key)`` 拼错键名时 TypeScript 立即报错，避免运行时才发现缺翻译。
 *
 * 注意：只推导最后一层为 string 的路径，跳过中间节点；最大深度 10 防爆栈。
 */

export type NestedKeyOf<T, Depth extends number[] = []> = Depth["length"] extends 10
  ? never
  : {
      [K in keyof T & string]: T[K] extends string
        ? `${K}`
        : T[K] extends object
          ? `${K}.${NestedKeyOf<T[K], [...Depth, 0]>}`
          : never;
    }[keyof T & string];

export type Vars = Readonly<Record<string, string | number | boolean>>;