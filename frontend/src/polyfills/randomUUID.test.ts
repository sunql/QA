import { describe, expect, it, vi } from "vitest";
import { installRandomUUIDPolyfill } from "./randomUUID";

function makeCryptoLike(overrides: Partial<Crypto> = {}): Crypto {
  return {
    getRandomValues: <T extends ArrayBufferView>(array: T): T => {
      const view = new Uint8Array(array.buffer, array.byteOffset, array.byteLength);
      for (let i = 0; i < view.length; i += 1) {
        view[i] = Math.floor(Math.random() * 256);
      }
      return array;
    },
    ...overrides,
  } as Crypto;
}

const UUID_V4_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe("installRandomUUIDPolyfill", () => {
  it("为缺少 randomUUID 的 crypto 补一个合法的 UUID v4 生成器", () => {
    const fakeCrypto = makeCryptoLike() as Crypto & { randomUUID?: () => string };
    expect(fakeCrypto.randomUUID).toBeUndefined();

    installRandomUUIDPolyfill(fakeCrypto);

    expect(typeof fakeCrypto.randomUUID).toBe("function");
    const value = fakeCrypto.randomUUID();
    expect(value).toMatch(UUID_V4_PATTERN);
  });

  it("生成的 uuid 两次调用互不相同", () => {
    const fakeCrypto = makeCryptoLike() as Crypto & { randomUUID?: () => string };
    installRandomUUIDPolyfill(fakeCrypto);

    expect(fakeCrypto.randomUUID()).not.toBe(fakeCrypto.randomUUID());
  });

  it("原生已有 randomUUID 时不覆盖", () => {
    const native = vi.fn(() => "native-value");
    const fakeCrypto = makeCryptoLike({ randomUUID: native as Crypto["randomUUID"] });

    installRandomUUIDPolyfill(fakeCrypto);

    expect(fakeCrypto.randomUUID()).toBe("native-value");
    expect(native).toHaveBeenCalledTimes(1);
  });

  it("连 getRandomValues 都没有时保持 no-op，不抛错", () => {
    const fakeCrypto = {} as Crypto;

    expect(() => installRandomUUIDPolyfill(fakeCrypto)).not.toThrow();
    expect((fakeCrypto as { randomUUID?: unknown }).randomUUID).toBeUndefined();
  });
});
