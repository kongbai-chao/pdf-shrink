---
name: pdf-shrink
description: "Use when a PDF must be squeezed under a hard size limit (报名/上传系统的 200KB、500KB 限制等) or when the user says 压缩/缩小 PDF、文件太大传不上去。Keeps text crisp by rebuilding the page as artwork + CCITT text stencil + image overlays, instead of flattening the whole page to one blurry JPEG. Works offline with the bundled Python + Pillow, no Ghostscript needed."
---

# PDF 体积压缩（保清晰度）

把一个 PDF 压到指定体积上限，尽量保住文字锐度、照片、二维码、印章。

## 什么时候用

- 报名/申报系统限制「单个附件 ≤ 200KB（或 500KB）」；
- 用户说「压缩 PDF」「文件太大传不上去」「压到 200K 以内」；
- 普通「另存为/在线压缩」把中文压花了，需要更精细的方案。

不适用：需要保留文字可选中、可搜索的场合（本方案会把文字转成 400dpi 位图蒙版）。

## 先跑，别手搓

脚本就在本技能目录里，直接调用（**不要**每次重新推导算法）：

```bash
PY="python"                                   # any Python 3.9+ with Pillow
SKILL="$HOME/.dsh/skills/pdf-shrink"          # or wherever this repo is cloned

# 1) 先复制到工作区（源文件常在工作区外，写盘要审批，读一般可以）
cp "/path/to/input.pdf" ./src.pdf

# 2) 压
"$PY" "$SKILL/pdf_shrink.py" src.pdf out.pdf --target-kb 200

# 3) 看输出的三行关键日志：dpi / visual diff / 最终字节数
```

`--target-kb` 是硬上限（按 1KB=1024B 判断）。常用：200、500、1024。

## 它做了什么（原理，便于调参和排错）

iText/报表类中文 PDF 的体积大头通常不是内容，而是**嵌入的中文子集字体**（单页常 200KB+）和**防伪底纹**。脚本把一页拆成三层重排：

1. **底纹层**：整页铺满的那张图（防伪边框 + 水印）。原样像素保留，只做调色板去重 + PNG 预测器 + Flate，通常 180KB → 150KB 左右，视觉无损。
2. **文字层**：把原 PDF 里所有图片绘制指令删掉后重新渲染一页，400dpi 二值化，用 **CCITT G4 蒙版**黑色盖上去。几十 KB 换掉两百多 KB 的字体。
3. **覆盖层**：照片、logo、二维码按原始矩形单独摆放。二维码/线稿走 1-bit Flate（1~2KB），照片走 JPEG；带 `/SMask` 透明度的 logo 会先合成到底纹上，避免出现白方块。

体积不够小就自动降级：400→380→…→200 dpi，再不行去掉底纹层，最后兜底整页 JPEG（`fallback_jpeg`，110~200dpi）。

## 必做校验（不要跳过）

1. **脚本自带**：压完会渲染源文件与产物同页对比，输出 `visual diff vs source: N/255`。> 12 就自动改用整页 JPEG 兜底（说明结构化解析不可靠，常见于 PDF 1.5+ 对象流文件）。
2. **人工再看一眼**：用技能同目录的渲染器把产物渲染成 PNG，view 它，确认：文字清晰、二维码完整、照片/印章在、没有丢页、没有白块。
   ```bash
   PS="/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
   "$PS" -NoProfile -ExecutionPolicy Bypass -File "$SKILL/render_page.ps1" -Pdf out.pdf -Out check.png -Width 2480
   ```
3. 报告用户：原始大小 → 新大小、视觉差异、以及「文字已转为图像」这个取舍。

## 环境注意事项

- 这台机器**没有** Ghostscript / qpdf / LibreOffice，也**没有外网**（pip 装不了）。渲染靠 Windows 自带的 `Windows.Data.Pdf`（`render_page.ps1`，PowerShell 5.1 + WinRT）。
- 沙箱里 `subprocess` 用管道捕获 stdout 会 `PermissionError`，脚本已改成写日志文件（`*.log`）。新写脚本时照做。
- 源文件在工作区外只能读；要写回桌面需要 `danger-full-access` 审批，一次调用只批一件。
- 输出路径不要用 `/tmp`（会被映射到沙箱 IPC 目录，写不进去）。写工作区内，再 `cp` 到目标位置。

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| `visual diff` 很大、页面缺底纹/照片 | 结构化解析没吃透（对象流/异常页树），脚本已自动兜底；若仍不对，用 `--target-kb` 放宽后走兜底路径 |
| 文字边缘锯齿明显 | 蒙版 dpi 太低。改脚本 attempts 里最高档，或临时把 `th` 阈值从 190 调到 170（笔画更实） |
| 压不到目标（最小产物 > 目标） | 内容真的多（多页扫描件/彩色照片）。告诉用户「这个体积内只能到 X KB」，或建议分卷/删页 |
| 二维码扫不出来 | 该图被判成 JPEG 了。在 `encode_overlay` 里把二值化判据放宽（`dark+light > 0.75` 调到 0.6） |
| 中文乱码/文件名报错 | 控制台是 GBK。脚本内部用 `mbcs` 兜底；自己拼命令行时避免在 heredoc 里写中文路径，改用 Python 的 `os.listdir` |

## 已知取舍（要主动告诉用户）

- 文字变成 400dpi 位图：**不能选中/搜索/复制**，打印和屏幕阅读正常。
- 防伪底纹、水印、边框都保留；只有「确实无法结构化解析」的文件会退化成整页位图（略糊）。
- 原文件绝不被覆盖；产物用 `_压缩版` 后缀。

## 需要 JPG/PNG 时（只收图片的上传系统）

有些系统（国企/事业单位/公考报名）附件只收 jpg/png。用同目录的 `pdf_to_image.py`，它按体积上限**挑最高 dpi**，而不是固定 dpi：

```bash
PY="python"
SKILL="$HOME/.dsh/skills/pdf-shrink"
"$PY" "$SKILL/pdf_to_image.py" in.pdf out_prefix --target-kb 200 [--format jpg] [--whiten 218]
```

- 多页会自动输出 `out_p1.jpg`、`out_p2.jpg`，**上传时要分开传**（推荐表、备案表常见两页）。
- 体积判据用 `target = kb*1024`；但很多系统按 `kb*1000` 卡。**留余量**：200KB 限额就用 `--target-kb 195`，300KB 用 `--target-kb 292`。
- 有红章/彩色证件照的页面**必须彩色**。判断方式：饱和像素（max-min>30）占比 ≥ 0.2% 即彩色；不要图省字节把带红章的页面转灰度。
- 纯文字页 220~280dpi 就能进 200KB；带防伪底纹的（验证报告、专利证书、带边框的证书）只能到 150~260dpi，必要时 `--whiten 218` 把极淡的暗纹抹白，换更高的文字分辨率。
- **踩过的坑**：多页探测靠「逐页渲染直到失败」，渲染前必须删除同名旧 PNG，否则会读到上一次残留的页，页数直接错。`render()` 里已加 `os.remove`。
