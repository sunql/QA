# PDF 中文字体（微软雅黑）

导出 PDF（会话问答导出、评估报告导出）优先使用**微软雅黑**：它会被嵌入子集，
浏览器内置查看器 / 手机端都能直接显示中文。缺字体文件时会回落 reportlab 内置的
`STSong-Light`（CID 字体，不嵌字形，部分阅读器显示方框）—— 导出不会失败。

## 为什么字体文件不在仓库里

微软雅黑不可再分发，**不要提交到 git**（`.gitignore` 已按 `docker/fonts/*.ttc` /
`*.ttf` 屏蔽）。由运维在每台部署机上自备。

## 放置

把 Windows 系统字体目录（`C:\Windows\Fonts`）下的两个文件复制到本目录：

```
docker/fonts/msyh.ttc      # Microsoft YaHei（正体）
docker/fonts/msyhbd.ttc    # Microsoft YaHei Bold（可选，缺了用正体代替粗体）
```

compose 把它只读挂到容器的 `/app/fonts`（基础文件 + CentOS overlay 都已配好；
CentOS 用 `:ro,z` 做 SELinux relabel）。**目录里的文件改了要重建容器才会重新挂载**：
修改的是内容而不是文件时，`docker compose restart backend` 即可。

也可以显式指定路径（绕过默认探测顺序）：

| 环境变量 | 含义 |
|---|---|
| `PDF_CJK_FONT_PATH` | 正体字体文件（`.ttc` 取第 0 个子字体，也接受 `.ttf`） |
| `PDF_CJK_BOLD_FONT_PATH` | 粗体字体文件；缺省或加载失败时粗体复用正体 |

默认探测顺序：`$PDF_CJK_FONT_PATH` → `/app/fonts/{msyh.ttc,msyh.ttf}` →
`docker/fonts/` → `backend/fonts/`（后两者用于宿主直接跑 uvicorn 的本地开发）。

## 验证

导出任意一份 PDF 后：

```bash
docker exec qa-backend python -c "from app.services.pdf_fonts import ensureCjkFont; print(ensureCjkFont())"
# 期望输出：MicrosoftYaHei（回落时输出 STSong-Light）
```

日志里会有 `PDF 中文字体：/app/fonts/msyh.ttc（粗体：/app/fonts/msyhbd.ttc）`；
显式路径写错、文件损坏都会打 warning 并回落，不会让导出 500。
