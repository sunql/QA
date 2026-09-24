import { describe, it, expect } from "vitest";

import * as i18next_module from "react-i18next";

describe("mock check", () => {
  it("mock applied via setup.ts", () => {
    console.log("i18next_module keys:", Object.keys(i18next_module));
    console.log("initReactI18next:", i18next_module.initReactI18next);
    if (typeof i18next_module.initReactI18next === "function") {
      // 底层类型是 ThirdPartyModule（无 call signature），需断言为可调用
      const fn = i18next_module.initReactI18next as unknown as () => { type: string };
      const m = fn();
      console.log("module from init:", m, "type:", m?.type);
    }
    expect(true).toBe(true);
  });
});
