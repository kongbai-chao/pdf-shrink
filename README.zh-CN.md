# pdf-shrink

把一个 PDF 压到指定体积，或者转成 JPG / PNG，**而且不让文字糊掉**。

常见的「压缩 PDF」要么把整页栅格化成一张 JPEG，要么粗暴地重子集化字体。中文文档更麻烦：
iText / reportlab 生成的 A4 单页常常带着 **230 KB 的内嵌中文字体子集** + **180 KB 的防伪底纹**，
一页文字就是 470 KB。

`pdf-shrink` 不压这张图，而是**把一页拆成三层重建**：把真正占体积的字体换成便宜的表示，
文字却保持锐利。

| 文件 | 压缩前 | 压缩后 |
|---|---|---|
| 学历在线验证报告 1 页 | 471 KB | **187 KB** |
| 研究生成绩单 1 页（纯矢量） | 244 KB | **32 KB** |
| 就业推荐表 2 页 | 451 KB | **189 KB** |
| 专利证书（防伪边框） | 1.0 MB | **167 KB** |

## 原理

```
          ┌──────────────────────────────────────────────────────────┐
 原始 PDF │ 底纹层：整页铺满的图，像素不动，只重编码调色板+PNG预测器   │ ≈150 KB
 (471 KB) │ 文字层：删掉所有图片绘制指令后渲染 400dpi → 1bit → G4 蒙版 │ ≈25 KB
          │ 覆盖层：照片 / logo / 二维码，按原始矩形单独摆放           │ ≈10 KB
          └──────────────────────────────────────────────────────────┘  → 187 KB
```

1. **底纹层**：整页那张背景图保留原始像素，只做调色板去重（23 → 13 色）+ PNG 行预测 + Flate
   （180 KB → 150 KB 左右），以 `/Indexed` + `/Predictor 15` 直接嵌入。
2. **文字层**：把内容流里所有图片的 `Do` 指令删掉后重新渲染，得到的只有文字和矢量线稿；
   二值化后用 **`/ImageMask` + CCITT G4** 作为蒙版，黑色盖在底纹上。几十 KB 就顶掉两百多 KB 的字体。
3. **覆盖层**：照片、校徽、二维码按原始 `cm` 矩阵切出来，摆回原来的矩形。二维码走 1-bit Flate
   （1~2 KB），照片走 JPEG；带 `/SMask` 透明度的 logo 会先合成到底纹上，不会出现白方块。

体积不达标就逐级降：400 → 380 → … → 200 dpi，再不行去掉底纹层，最后整页 JPEG 兜底。

### 它会自己验收

每次运行都会把源文件和产物渲染成同尺寸的图做像素比对：

```
  dpi=400 art=True -> 191029 bytes (187 KB)
  visual diff vs source: 2.2/255
```

超过 `12/255` 就判定重建失败（PDF 1.5+ 把页树藏在对象流里的文件常出现这种情况），
**自动退回整页位图**，而不是交付一个少了照片或公章的页面。

## 环境要求

- **Windows 10/11**：渲染走系统自带的 `Windows.Data.Pdf`（PowerShell + WinRT），
  不需要装 Ghostscript / poppler / MuPDF。
- **Python 3.9+** 和 **Pillow**（`pip install pillow`）。除 Pillow 外只用标准库。

## 用法

### PDF 压 PDF

```bash
python pdf_shrink.py input.pdf output.pdf --target-kb 200
```

### PDF 转图片（只收 jpg/png 的报名系统）

```bash
python pdf_to_image.py input.pdf out_prefix --target-kb 195
```

它按**能塞进预算的最高 dpi** 出图，而不是固定 dpi：

```
rec.pdf: 2 page(s), target <= 195 KB
  page 1 -> rec_p1.jpg  194.6 KB  1654x2341  dpi=200 q=52 color
  page 2 -> rec_p2.jpg  194.6 KB  1819x2574  dpi=220 q=60 color
```

多页会输出 `out_p1.jpg`、`out_p2.jpg`，**上传时要分开传**。

| 参数 | 作用 |
|---|---|
| `--target-kb N` | 体积上限 |
| `--whiten 218` | 把比 218 更亮的像素抹白，去掉极淡的暗纹，换来约 20% 的文字分辨率 |
| `--format png` | 只在系统强制 PNG 时用（扫描件一般 JPEG 更小） |
| `--gray auto` | 有红章或彩色证件照的页面自动保留彩色 |

## 字节口径的坑

系统写「200KB」，可能指 204 800 字节，也可能指 200 000 字节。**留余量**：

```bash
python pdf_to_image.py in.pdf out --target-kb 195   # 200KB 限额
python pdf_to_image.py in.pdf out --target-kb 292   # 300KB 限额
```

## 已知限制

- **文字会变成图像**：不可选中、不可搜索、不可复制。打印和屏幕阅读不受影响。
  一页带着 230 KB 内嵌字体的文档，要在 200 KB 以内只能这样。
- **目前只支持 Windows**。两个脚本调用 `render_page.ps1`；把这一层换成
  `pdftoppm` / `mutool draw` 就能跨平台，其余流程与平台无关。
- PDF 1.5+ 若把页树放在对象流里，只做了 `/ObjStm` 展开（未实现 xref stream），
  解析不出来时退回整页位图。
- 它不是一个 PDF 重写器：只重建它认得出的那些图层。

## 目录结构

```
pdf_shrink.py          PDF → 更小的 PDF（分层重建）
pdf_to_image.py        PDF → JPG/PNG（按体积预算）
render_page.ps1        单页 → PNG，走 Windows.Data.Pdf
docs/algorithm.md      实测数据、参数选择、走过的弯路
integrations/dsh/      给 DeepSeek Harness 用的 SKILL.md
```

## 许可证

MIT，见 [LICENSE](LICENSE)。
