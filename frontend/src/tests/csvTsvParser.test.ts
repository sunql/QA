import { describe, it, expect } from "vitest";
import {
  parseDelimited,
  parseLine,
  rowsToRecords,
} from "../utils/csvTsvParser";

describe("utils/csvTsvParser - parseLine", () => {
  it("splits simple CSV on commas", () => {
    expect(parseLine("a,b,c", ",")).toEqual(["a", "b", "c"]);
  });

  it("splits on Tab when delimiter is \\t", () => {
    expect(parseLine("a\tb\tc", "\t")).toEqual(["a", "b", "c"]);
  });

  it("keeps empty fields (consecutive delimiters)", () => {
    expect(parseLine("a,,c", ",")).toEqual(["a", "", "c"]);
  });

  it("respects double-quoted fields containing delimiter", () => {
    expect(parseLine('"a,b",c', ",")).toEqual(["a,b", "c"]);
  });

  it("decodes escaped double-quote as \"", () => {
    expect(parseLine('"a""b",c', ",")).toEqual(['a"b', "c"]);
  });

  it("handles quote spanning whole field", () => {
    expect(parseLine('"hello",world', ",")).toEqual(["hello", "world"]);
  });

  it("preserves trailing empty field", () => {
    expect(parseLine("a,b,", ",")).toEqual(["a", "b", ""]);
  });
});

describe("utils/csvTsvParser - parseDelimited", () => {
  it("strips UTF-8 BOM", () => {
    const text = "﻿a,b\n1,2";
    const r = parseDelimited(text);
    expect(r.headers).toEqual(["a", "b"]);
    expect(r.rows).toEqual([["1", "2"]]);
  });

  it("prefers Tab delimiter when both Tab and comma present (Tab wins)", () => {
    const text = "h1\th2\th3\n1,2\t3\t4";
    const r = parseDelimited(text);
    // tab-delimited should give 3 columns all-matching
    expect(r.delimiter).toBe("\t");
    expect(r.headers).toEqual(["h1", "h2", "h3"]);
  });

  it("falls back to comma when Tab is not the consistent delimiter", () => {
    const text = "h1,h2\n1,2\n3,4";
    const r = parseDelimited(text);
    expect(r.delimiter).toBe(",");
    expect(r.headers).toEqual(["h1", "h2"]);
    expect(r.rows).toEqual([
      ["1", "2"],
      ["3", "4"],
    ]);
  });

  it("ignores trailing empty line", () => {
    const text = "a,b\n1,2\n";
    const r = parseDelimited(text);
    expect(r.rows).toEqual([["1", "2"]]);
  });

  it("emits warning when a row has column count mismatch", () => {
    const text = "a,b,c\n1,2\n3,4,5,6";
    const r = parseDelimited(text);
    // 表头 3 列；行 1 (2 列) 与 行 2 (4 列) 都偏离 → 两条警告；rows 都丢弃
    expect(r.warnings.length).toBeGreaterThanOrEqual(2);
    expect(r.rows).toEqual([]);
  });

  it("returns empty result for empty input", () => {
    const r = parseDelimited("");
    expect(r.headers).toEqual([]);
    expect(r.rows).toEqual([]);
    expect(r.warnings).toContain("empty input");
  });
});

describe("utils/csvTsvParser - rowsToRecords", () => {
  it("maps rows to records with 1-based row numbers (data starts at 2)", () => {
    const text = "name,age\nAlice,30\nBob,25";
    const records = rowsToRecords(parseDelimited(text));
    expect(records).toEqual([
      { row: 2, values: { name: "Alice", age: "30" } },
      { row: 3, values: { name: "Bob", age: "25" } },
    ]);
  });
});