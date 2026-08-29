import { describe, it, expect } from "vitest";
import { toCsv } from "../utils/download";

describe("toCsv 纯函数", () => {
  it("空数组返回空串", () => {
    expect(toCsv([])).toBe("");
  });

  it("表头取首行 key 且顺序不变", () => {
    const csv = toCsv([{ 物料: "A", 数量: 2 }]);
    expect(csv).toContain("﻿物料,数量\r\nA,2");
  });

  it("以 BOM（﻿）开头，Excel 可直开不乱码", () => {
    expect(toCsv([{ a: 1 }]).charCodeAt(0)).toBe(0xfeff);
  });

  it("含逗号的字段用双引号包裹", () => {
    expect(toCsv([{ name: "x,y" }])).toBe('﻿name\r\n"x,y"');
  });

  it("含双引号的字段转义为两个双引号", () => {
    expect(toCsv([{ name: 'he said "hi"' }])).toBe('﻿name\r\n"he said ""hi"""');
  });

  it("含换行的字段用双引号包裹，嵌入换行原样保留", () => {
    expect(toCsv([{ name: "line1\nline2" }])).toBe('﻿name\r\n"line1\nline2"');
  });

  it("null / undefined 转为空串", () => {
    expect(toCsv([{ a: null, b: undefined, c: "x" }])).toBe("﻿a,b,c\r\n,,x");
  });

  it("多行数据逐行输出，行分隔为 CRLF", () => {
    expect(toCsv([{ a: 1 }, { a: 2 }])).toBe("﻿a\r\n1\r\n2");
  });
});
