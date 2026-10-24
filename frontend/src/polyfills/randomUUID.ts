// HTTP 非安全上下文（如 http://<服务器IP>:5173）下 crypto.randomUUID 不存在，
// 会导致 wikiChatStore 在模块加载期就抛 TypeError（白屏）。此处用
// getRandomValues 补一个 UUID v4 实现；HTTPS / localhost 原生存在时不动作。
//
// 注意：正式部署配了 HTTPS 后本 polyfill 自动变为 no-op，可长期保留。

function formatUuidV4(bytes: Uint8Array): string {
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}

export function installRandomUUIDPolyfill(browserCrypto: Crypto): void {
  if (typeof browserCrypto.randomUUID === "function") {
    return;
  }
  if (typeof browserCrypto.getRandomValues !== "function") {
    return;
  }

  Object.defineProperty(browserCrypto, "randomUUID", {
    configurable: true,
    writable: true,
    value: function randomUUID(): string {
      const bytes = browserCrypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      return formatUuidV4(bytes);
    },
  });
}

if (typeof globalThis.crypto !== "undefined") {
  installRandomUUIDPolyfill(globalThis.crypto);
}
