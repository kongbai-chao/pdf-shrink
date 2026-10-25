#!/usr/bin/env python3
"""
pdf_to_image.py -- turn PDF pages into JPG/PNG images under a byte budget.

Portals that only accept images (jpg/jpeg/png) still impose a size cap, so this
picks the sharpest (highest dpi) encoding that fits the budget instead of
blindly shrinking to a fixed dpi.

  python pdf_to_image.py IN.pdf OUT_PREFIX --target-kb 200 [--format jpg|png]
                          [--max-dpi 400] [--whiten 0|218] [--page N]

Outputs OUT_PREFIX.jpg, or OUT_PREFIX_p1.jpg / _p2 ... for multi-page files.
"""
import argparse
import io
import os
import subprocess
import sys

from PIL import Image

PS = os.environ.get("SystemRoot", r"C:\Windows") + r"\System32\WindowsPowerShell\v1.0\powershell.exe"
HERE = os.path.dirname(os.path.abspath(__file__))


def render(pdf, page, out_png, width, ps_script):
    cmd = [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_script,
           "-Pdf", pdf, "-Out", out_png, "-Width", str(int(width)), "-Page", str(page)]
    # never trust a file left over from an earlier run
    if os.path.exists(out_png):
        os.remove(out_png)
    log = out_png + ".log"
    with open(log, "wb") as lf:
        subprocess.run(cmd, stdout=lf, stderr=lf)
    if not os.path.exists(out_png):
        txt = open(log, "rb").read()[-1200:].decode("mbcs", "replace")
        raise RuntimeError("render failed for page %d:\n%s" % (page, txt))
    im = Image.open(out_png)
    want = int(round(width))
    if im.width > want * 1.02:
        im = im.resize((want, int(round(im.height * want / im.width))), Image.LANCZOS)
    return im


def page_size(im, dpi):
    w = int(round(dpi * 8.2677))
    if im.width < w:
        w = im.width
    return w, max(1, int(round(im.height * w / im.width)))


def encode(im, fmt, q):
    b = io.BytesIO()
    if fmt == "png":
        im.save(b, "PNG", optimize=True)
    else:
        im.save(b, "JPEG", quality=q, optimize=True, subsampling=1)
    return b.getvalue()


def is_gray(im):
    """true only when the page has no meaningful colour (red seals must stay colour)"""
    small = im.convert("RGB").resize((200, 200))
    px = list(small.getdata())
    colored = 0
    for p in px:
        if max(p) - min(p) > 30:
            colored += 1
    return colored / float(len(px)) < 0.002


def best_fit(master, target, fmt, args):
    """walk dpi from high to low, quality high to low, return the first that fits"""
    best = None
    for dpi in (args.max_dpi, 360, 330, 300, 280, 260, 240, 220, 200, 180, 165, 150, 135, 120, 105, 96):
        if dpi > args.max_dpi:
            continue
        w, h = page_size(master, dpi)
        small = master.resize((w, h), Image.LANCZOS) if (w, h) != master.size else master
        variants = [small]
        if args.whiten:
            variants.append(small.point(lambda v: 255 if v >= args.whiten else v))
        for vi, img in enumerate(variants):
            rgb = img.convert("RGB")
            if args.gray == "auto" and is_gray(rgb):
                rgb = rgb.convert("L")
            elif args.gray == "yes":
                rgb = rgb.convert("L")
            for q in (92, 88, 84, 80, 76, 72, 68, 64, 60, 56, 52):
                if fmt == "png" and q != 92:
                    continue
                data = encode(rgb, fmt, q)
                if len(data) <= target:
                    return dict(data=data, dpi=dpi, q=q, size=rgb.size, gray=rgb.mode == "L", variant=vi)
                if best is None:
                    best = dict(data=data, dpi=dpi, q=q, size=rgb.size, gray=rgb.mode == "L", variant=vi)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out_prefix")
    ap.add_argument("--target-kb", type=int, default=200)
    ap.add_argument("--format", default="jpg", choices=["jpg", "png"])
    ap.add_argument("--max-dpi", type=int, default=400)
    ap.add_argument("--whiten", type=int, default=0, help="0 = keep texture, 218 = drop the faint watermark")
    ap.add_argument("--gray", default="auto", choices=["auto", "yes", "no"])
    ap.add_argument("--page", type=int, default=-1, help="-1 = all pages")
    ap.add_argument("--renderer", default=os.path.join(HERE, "render_page.ps1"))
    ap.add_argument("--workdir", default="_pdf2img")
    a = ap.parse_args()
    os.makedirs(a.workdir, exist_ok=True)
    target = a.target_kb * 1024

    # discover pages
    pages = []
    idx = 0
    while True:
        if a.page >= 0 and a.page != idx:
            break
        try:
            tag = os.path.splitext(os.path.basename(a.src))[0]
            im = render(a.src, idx, os.path.join(a.workdir, "%s_p%d.png" % (tag, idx)),
                        a.max_dpi * 8.2677, a.renderer)
        except Exception:
            break
        pages.append(im)
        idx += 1
        if idx > 60:
            break
    if not pages:
        raise SystemExit("no pages rendered from %s" % a.src)

    print("%s: %d page(s), target <= %d KB" % (os.path.basename(a.src), len(pages), a.target_kb))
    for i, master in enumerate(pages):
        r = best_fit(master, target, a.format, a)
        if r is None:
            print("  page %d: cannot fit" % (i + 1))
            continue
        ext = ".png" if a.format == "png" else ".jpg"
        name = a.out_prefix + (ext if len(pages) == 1 else "_p%d%s" % (i + 1, ext))
        open(name, "wb").write(r["data"])
        print("  page %d -> %s  %.1f KB  %dx%d  dpi=%d q=%d %s%s" % (
            i + 1, os.path.basename(name), len(r["data"]) / 1024.0, r["size"][0], r["size"][1],
            r["dpi"], r["q"], "gray" if r["gray"] else "color",
            " (whitened)" if r["variant"] else ""))
        if len(r["data"]) > target:
            print("    WARNING: smallest possible still exceeds the target")
    return 0


if __name__ == "__main__":
    sys.exit(main())
