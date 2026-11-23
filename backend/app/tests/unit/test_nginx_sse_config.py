"""`docker/nginx.conf` 的 /api/ 反代必须 SSE 安全。

为什么要有这条守卫：2026-11-23 生产 Copilot 流式对话收尾时 Chrome 报
`net::ERR_INCOMPLETE_CHUNKED_ENCODING`。根因是 /api/ 反代用了 nginx 默认的
HTTP/1.0 上游（无长度、靠关连接定界），收尾时客户端拿不到终止块。

这条配置**不会被任何后端单测覆盖**（它只在镜像里生效），所以用一个静态守卫钉住：
少一行 `proxy_http_version 1.1` 或 `Connection ""`，SSE 就会退回「偶发截断」。

注意（同 `qa-system-guard-vacuous-pass-lib-upgrade` 的教训）：守卫必须带**数量下限**，
否则 location 段被重命名/删除时断言会变成空集通过。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
NGINX_CONF = REPO / "docker" / "nginx.conf"

#: `/api/` location 段里必须出现的指令（含空 Connection 头）。
REQUIRED_API_DIRECTIVES = [
    "proxy_buffering off;",
    "proxy_request_buffering off;",
    'proxy_http_version 1.1;',
    'proxy_set_header Connection "";',
    "proxy_read_timeout 300s;",
]

#: 超时不得低于 Copilot 一次 SSE 的等待预算（120s）的两倍余量。
MIN_PROXY_READ_TIMEOUT_SECONDS = 240


def _apiLocationBlock() -> str:
    """取出 `location /api/ { ... }` 的整段（按花括号配平）。"""
    text = NGINX_CONF.read_text(encoding="utf-8")
    match = re.search(r"location\s+/api/\s*\{", text)
    assert match is not None, "nginx.conf 里找不到 location /api/ 段"

    depth = 0
    start = match.end() - 1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError("location /api/ 段花括号不配平")


class TestNginxSseConfig:
    def test_api_block_has_enough_directives(self) -> None:
        """段内指令数量下限：防止 location 改名后守卫空集通过。"""
        block = _apiLocationBlock()
        directives = [
            line.strip()
            for line in block.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert len(directives) >= 10, (
            f"/api/ 段只解析到 {len(directives)} 条有效指令，疑似段结构变了"
        )

    def test_api_block_carries_sse_safe_directives(self) -> None:
        block = _apiLocationBlock()
        missing = [item for item in REQUIRED_API_DIRECTIVES if item not in block]
        assert not missing, (
            f"/api/ 反代缺少 SSE 安全指令：{missing}（缺 proxy_http_version 1.1 "
            "会在流式收尾时报 ERR_INCOMPLETE_CHUNKED_ENCODING）"
        )

    def test_proxy_read_timeout_covers_stream_budget(self) -> None:
        block = _apiLocationBlock()
        match = re.search(r"proxy_read_timeout\s+(\d+)s;", block)
        assert match is not None, "/api/ 段缺少 proxy_read_timeout"
        seconds = int(match.group(1))
        assert seconds >= MIN_PROXY_READ_TIMEOUT_SECONDS, (
            f"proxy_read_timeout={seconds}s 低于 {MIN_PROXY_READ_TIMEOUT_SECONDS}s，"
            "长时间无字节的流式响应会被提前掐断"
        )
