"""导出 PDF 的防御性上界（0105 起；2026-10-31 增加请求体级上界）。

**为什么放 domain**：``schemas`` 在**解析阶段**就要拒绝超限请求体（100 MB 的
JSON 全文读入之后再由 service 拒绝，已经付过解析开销），而
``session_history_service`` 在**解码阶段**还要按同一组数字把关。两处各写一份
必然漂移：schema 放行、service 拒绝，或反过来把闸门开成摆设。所以数字只在这里
定义一次。

这些是**防御性上界，不是可调策略**（按《魔数治理》「判别不清」档留在源码里，
不进 ``system_config``）：前端真实产物是 1600 × 2H（CSS 高度 320 起、960 封顶），
上限留 2 倍以上余量即可；运营没有任何理由需要调它。
"""

from __future__ import annotations

# 单张 PNG **解码后**的字节上限
MAX_EXPORT_IMAGE_BYTES = 2 * 1024 * 1024
# charts 条数上限
MAX_EXPORT_CHART_IMAGES = 200
# 全部图片解码后的总量上限
MAX_EXPORT_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024

# 字节上限只管**压缩后**大小，管不住解出来有多大：一张 12000×12000 的纯色 PNG
# 只有 580 KB，交给 Pillow 却要按 1.44 亿像素分配内存（实测单张峰值 RSS +2 GB）。
# 所以像素数单独卡。上界取 8M px（≈4000×2000），前端最大约 1600×1920 = 3.07M px；
# 总量 40M px 把一次请求的瞬时解码内存压在 ~160 MB。
MAX_EXPORT_IMAGE_PIXELS = 8_000_000
MAX_EXPORT_TOTAL_IMAGE_PIXELS = 40_000_000

# 只收 PNG（前端 getDataURL({type:"png"}) 的输出）。前缀 + 魔数双重校验：
# 只信前缀等于允许「把任意二进制贴个 PNG 标签」送进来。
CHART_IMAGE_DATA_URL_PREFIX = "data:image/png;base64,"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
