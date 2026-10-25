#!/usr/bin/env python3
"""
pdf_shrink.py -- shrink a PDF to a byte budget while keeping text crisp.

Strategy ("layered rebuild"):
  * the decorative background artwork (the image drawn across the whole page)
    is re-encoded losslessly-ish (palette-dedup + PNG predictor + Flate);
  * every other image (photo, logo, QR) is re-encoded as its own JPEG layer
    drawn at its original rectangle;
  * everything else (text, vector art) is rendered once at high DPI, thresholded
    to 1 bit and stored as a CCITT G4 stencil mask painted black on top.

This removes the dominant cost of iText/python-reportlab CJK PDFs: a fat
embedded CJK font subset (often 200 KB+ for one page).

Usage:
  python pdf_shrink.py IN.pdf OUT.pdf --target-kb 200 [--workdir DIR]
                                       [--renderer render_page.ps1]
                                       [--keep-work]
"""
import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import zlib

from PIL import Image, ImageChops

ALPHAS = {}

PS = os.environ.get("SystemRoot", r"C:\Windows") + r"\System32\WindowsPowerShell\v1.0\powershell.exe"


# --------------------------------------------------------------------------- pdf
class Pdf:
    def __init__(self, data):
        self.data = data
        self.objs = {}
        self.embedded = {}
        for m in re.finditer(rb"(?m)^(\d+) 0 obj", data):
            n = int(m.group(1))
            s = m.start()
            e = data.find(b"endobj", s)
            self.objs[n] = data[s:e + 6]
        tail = data[data.rfind(b"trailer"):]
        m = re.search(rb"/Root (\d+) 0 R", tail)
        self.root = int(m.group(1)) if m else 1
        m = re.search(rb"/Info (\d+) 0 R", tail)
        self.info = int(m.group(1)) if m else None
        self._expand_object_streams()
        # some PDFs put /Root only inside an xref stream dictionary or an ObjStm
        if self.root not in self.objs and self.root not in self.embedded:
            for n in self.objs:
                if re.search(rb"/Type\s*/Catalog", self.dict_of(n)):
                    self.root = n
                    break

    def _expand_object_streams(self):
        """objects inside /Type /ObjStm streams (PDF 1.5+) are needed for page trees"""
        for n in list(self.objs):
            d = self.dict_of(n)
            if not re.search(rb"/Type\s*/ObjStm", d):
                continue
            try:
                m = re.search(rb"/N\s+(\d+)", d)
                f = re.search(rb"/First\s+(\d+)", d)
                if not (m and f):
                    continue
                count, first = int(m.group(1)), int(f.group(1))
                body = self.stream(n)
                header = body[:first].split()
                pairs = [(int(header[i]), int(header[i + 1])) for i in range(0, min(len(header), count * 2), 2)]
                for idx, (num, off) in enumerate(pairs):
                    end = pairs[idx + 1][1] if idx + 1 < len(pairs) else len(body) - first
                    self.embedded[num] = body[first + off:first + end]
            except Exception:
                continue

    def has(self, n):
        return n in self.objs or n in self.embedded

    def dict_of(self, n):
        if n in self.objs:
            chunk = self.objs[n]
            i = chunk.find(b"stream")
            return chunk[:i] if i >= 0 else chunk
        return self.embedded[n]

    def payload(self, n):
        chunk = self.objs[n]
        i = chunk.find(b"stream")
        if i < 0:
            return b""
        j = chunk.find(b"endstream", i)
        p = chunk[i + 6:j]
        if p.startswith(b"\r\n"):
            p = p[2:]
        elif p.startswith(b"\n"):
            p = p[1:]
        return p

    def stream(self, n):
        p = self.payload(n)
        if b"/FlateDecode" in self.dict_of(n):
            return zlib.decompress(p)
        return p

    def pages(self):
        """walk /Root -> /Pages -> /Kids so page order is preserved"""
        seen = set()
        out = []

        def kids(num):
            if num in seen or not self.has(num):
                return
            seen.add(num)
            d = self.dict_of(num)
            if re.search(rb"/Type\s*/Page[^s]", d):
                out.append(num)
                return
            m = re.search(rb"/Kids\s*\[(.*?)\]", d, re.S)
            if not m:
                return
            for mm in re.finditer(rb"(\d+)\s+0\s+R", m.group(1)):
                kids(int(mm.group(1)))

        root = self.dict_of(self.root) if self.has(self.root) else b""
        m = re.search(rb"/Pages\s+(\d+)\s+0\s+R", root)
        if m:
            kids(int(m.group(1)))
        if not out:
            for n in sorted(list(self.objs) + list(self.embedded)):
                if re.search(rb"/Type\s*/Page[^s]", self.dict_of(n)):
                    out.append(n)
        # keep first occurrence only: a duplicated kid would otherwise repeat a page
        uniq = []
        for n in out:
            if n not in uniq:
                uniq.append(n)
        return uniq


def page_media(data, d):
    m = re.search(rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)", d)
    if not m:
        return 0.0, 0.0, 595.0, 842.0
    a, b, c, dd = (float(x) for x in m.groups())
    return a, b, c - a, dd - b


IMG_DRAW = re.compile(
    rb"q\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+cm\s*/(\w+)\s+Do\s*Q")


def page_layers(pdf, page_num):
    """-> (background (name, objnum, rect) or None, overlays [ (name,obj,rect) ],
           content_bytes_without_image_draws, media(0,0,w,h))"""
    d = pdf.dict_of(page_num)
    media = page_media(pdf.data, d)
    m = re.search(rb"/Contents\s+(\d+) 0 R", d)
    content = pdf.stream(int(m.group(1)))
    res = re.search(rb"/XObject\s*<<(.*?)>>", d, re.S)
    xmap = {}
    if res:
        for mm in re.finditer(rb"/(\w+)\s+(\d+) 0 R", res.group(1)):
            xmap[mm.group(1)] = int(mm.group(2))

    pw, ph = media[2], media[3]
    bg = None
    overlays = []
    for mm in IMG_DRAW.finditer(content):
        a, b, c, dd, e, f = (float(x) for x in mm.groups()[:6])
        name = mm.group(7)
        rect = (e, f, abs(a), abs(dd))
        covers = abs(a) >= pw * 0.9 and abs(dd) >= ph * 0.9
        entry = (name, xmap.get(name), rect)
        if covers and bg is None:
            bg = entry
        else:
            overlays.append(entry)
    stripped = IMG_DRAW.sub(b"", content)
    return bg, overlays, stripped, media


# ----------------------------------------------------------------- image decode
def decode_image(pdf, num):
    """-> PIL image (RGB or P) + original palette if indexed, else None"""
    d = pdf.dict_of(num)
    raw = pdf.stream(num)
    w = int(re.search(rb"/Width\s+(\d+)", d).group(1))
    h = int(re.search(rb"/Height\s+(\d+)", d).group(1))
    if b"/DCTDecode" in d:
        return Image.open(io.BytesIO(pdf.payload(num))).convert("RGB")
    sub = re.search(rb"/ColorSpace\s*/(\w+)", d)
    sub = sub.group(1) if sub else b"DeviceRGB"
    indexed = b"/Indexed" in d
    bpc = 8
    m = re.search(rb"/BitsPerComponent\s+(\d+)", d)
    if m:
        bpc = int(m.group(1))
    if bpc != 8:
        raise ValueError("bpc %d not supported" % bpc)
    if indexed:
        pal = re.search(rb"/Indexed\s*/DeviceRGB\s+\d+\s*<([0-9A-Fa-f\s]+)>", d)
        if pal:
            palette = bytes.fromhex(re.sub(rb"\s", b"", pal.group(1)).decode())
        else:
            k = d.find(b"DeviceRGB")
            lp = d.find(b"(", k)
            rp = d.find(b")", lp)
            palette = d[lp + 1:rp]
        cols = w
        dp = re.search(rb"/Columns\s+(\d+)", d)
        pred = b"/Predictor" in d
        if pred:
            cols = int(dp.group(1))
            raw = png_unpredict(raw, cols, h)
        raw = raw[:cols * h]
        im = Image.frombytes("P", (cols, h), raw)
        im.putpalette(palette[:768])
        if cols != w:
            im = im.resize((w, h))
        return im
    mode = "L" if sub == b"DeviceGray" else "RGB"
    im = Image.frombytes(mode, (w, h), raw[:w * h * len(mode)])
    return apply_smask(pdf, num, d, im)


def png_unpredict(data, columns, rows):
    out = bytearray()
    prev = bytearray(columns)
    pos = 0
    for _ in range(rows):
        ft = data[pos]
        pos += 1
        row = bytearray(data[pos:pos + columns])
        pos += columns
        if ft == 1:
            for i in range(1, columns):
                row[i] = (row[i] + row[i - 1]) & 0xFF
        elif ft == 2:
            for i in range(columns):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(columns):
                left = row[i - 1] if i else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(columns):
                a = row[i - 1] if i else 0
                b = prev[i]
                c = prev[i - 1] if i else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pr) & 0xFF
        out += row
        prev = row
    return bytes(out)


# ------------------------------------------------------------------ pdf writing
CRLF = bytes([13, 10])


def strip_wrapper(chunk):
    """turn 'N 0 obj ... endobj' into the bare body (build_pdf adds the wrapper)"""
    m = re.match(rb"\s*\d+\s+\d+\s+obj", chunk)
    if m:
        chunk = chunk[m.end():]
    k = chunk.rfind(b"endobj")
    if k >= 0:
        chunk = chunk[:k]
    return chunk.strip(bytes([13, 10]))


def build_pdf(objects, root, info=None, title=None):
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offs = {}
    for num, body in objects:
        offs[num] = len(out)
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    size = max(offs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for n in range(1, size):
        if n in offs:
            out += b"%010d 00000 n \n" % offs[n]
        else:
            out += b"0000000000 65535 f \n"
    tr = b"trailer\n<</Size %d /Root %d 0 R" % (size, root)
    if info:
        tr += b" /Info %d 0 R" % info
    tr += b">>\nstartxref\n%d\n%%%%EOF\n" % xref
    out += tr
    return bytes(out)


def image_obj(i, extra, data):
    return (i, b"<<" + extra + b" /Length %d>>\nstream\n" % len(data) + data + b"\nendstream")


def g4_of(mask_bits):
    """encode a mode-'1' image as CCITT G4; return (data, w, h) reusing Pillow's writer"""
    buf = io.BytesIO()
    mask_bits.save(buf, "PDF", resolution=400.0)
    ppdf = buf.getvalue()
    m = re.search(rb"/Filter\s*\[?\s*/CCITTFaxDecode", ppdf)
    if not m:
        raise RuntimeError("pillow did not emit CCITT")
    s = ppdf.rfind(b"\n", 0, ppdf.rfind(b" obj", 0, m.start())) + 1
    e = ppdf.find(b"endobj", m.start())
    chunk = ppdf[s:e]
    i = chunk.find(b"stream")
    j = chunk.find(b"endstream", i)
    data = chunk[i + 6:j].lstrip(b"\r\n")
    return data


# ------------------------------------------------------------------- rendering
def render(ps_script, pdf_path, page, out_png, width):
    cmd = [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_script,
           "-Pdf", pdf_path, "-Out", out_png, "-Width", str(int(width)), "-Page", str(page)]
    # NOTE: piped stdio can be blocked inside a sandbox, so log to a file instead.
    log = out_png + ".log"
    with open(log, "wb") as lf:
        subprocess.run(cmd, stdout=lf, stderr=lf)
    if not os.path.exists(out_png):
        txt = open(log, "rb").read()[-1500:].decode("mbcs", "replace")
        raise RuntimeError("render failed:\n" + txt)
    im = Image.open(out_png)
    # the DPI -> DIP request gets scaled by the desktop scaling factor, so normalise
    want = int(round(width))
    if im.width > want * 1.02:
        im = im.resize((want, int(round(im.height * want / im.width))), Image.LANCZOS)
    return im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--target-kb", type=int, default=200)
    ap.add_argument("--workdir", default="_pdfshrink")
    ap.add_argument("--renderer", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                       "render_page.ps1"))
    ap.add_argument("--keep-work", action="store_true")
    a = ap.parse_args()
    target = a.target_kb * 1024
    os.makedirs(a.workdir, exist_ok=True)
    data = open(a.src, "rb").read()
    pdf = Pdf(data)
    pages = pdf.pages()
    print("pages:", pages, "objects:", len(pdf.objs), "embedded:", len(pdf.embedded))

    if not pages:
        print("no page tree found -> raster fallback")
        out = fallback_jpeg(pdf, [], a, target)
        if len(out) < 1024:
            raise RuntimeError("fallback produced an empty PDF; aborting")
        open(a.out, "wb").write(out)
        print("wrote %s  %d bytes = %.1f KB" % (a.out, len(out), len(out) / 1024))
        return 0

    # ---- classify layers per page, and build a mask-source PDF (text+vector only)
    pageinfo = []
    for idx, pn in enumerate(pages):
        bg, overlays, stripped, media = page_layers(pdf, pn)
        pageinfo.append(dict(num=pn, bg=bg, overlays=overlays, media=media, idx=idx))
        print(" page %d: media %s bg=%s overlays=%s" % (idx, media, bool(bg), len(overlays)))

    mask_src = dict(pdf.objs)
    mask_src.update(pdf.embedded)
    # rewrite each page content stream without the image draws (text/vector only)
    for pi in pageinfo:
        d = pdf.dict_of(pi["num"])
        m = re.search(rb"/Contents\s+(\d+) 0 R", d)
        if not m:
            raise RuntimeError("page %d has no single /Contents stream" % pi["idx"])
        cnum = int(m.group(1))
        pi["content_obj"] = cnum
        bg, overlays, stripped, media = page_layers(pdf, pi["num"])
        pi["stripped"] = stripped
        comp = zlib.compress(stripped, 9)
        chunk = pdf.objs[cnum]
        i = chunk.find(b"stream")
        head = re.sub(rb"/Length \d+", b"/Length %d" % len(comp), chunk[:i])
        tail = chunk[chunk.find(b"endstream"):]
        mask_src[cnum] = head + b"stream" + CRLF + comp + CRLF + b"endstream" + tail

    mask_pdf = build_pdf([(n, strip_wrapper(c)) for n, c in sorted(mask_src.items())],
                        pdf.root, pdf.info)
    mask_path = os.path.join(a.workdir, "mask_src.pdf")
    open(mask_path, "wb").write(mask_pdf)

    # ---- artwork encodings (computed once)
    arts = {}
    for pi in pageinfo:
        if not pi["bg"]:
            continue
        num = pi["bg"][1]
        if num in arts:
            continue
        im = decode_image(pdf, num)
        arts[num] = im
    print("artwork images:", {k: v.size for k, v in arts.items()})

    # ---- overlays
    ovs = {}
    for pi in pageinfo:
        for name, num, rect in pi["overlays"]:
            if num and num not in ovs:
                try:
                    ovs[num] = decode_image(pdf, num)
                except Exception as e:
                    print("  overlay %s skipped (%s)" % (num, e))

    # ---- try progressively cheaper encodings until we fit
    attempts = [dict(dpi=d, art=True, art_colors=16, art_keep=False, q=78)
                for d in (400, 380, 350, 320, 300, 280, 260, 240, 220, 200)]
    attempts += [dict(dpi=d, art=False, art_colors=0, art_keep=False, q=72)
                 for d in (320, 280, 240, 200)]

    renders = {}
    best = None
    for at in attempts:
        dpi = at["dpi"]
        # render masks at this dpi
        for pi in pageinfo:
            key = (pi["idx"], dpi)
            if key in renders:
                continue
            w = dpi * 8.2677
            img = render(a.renderer, mask_path, pi["idx"],
                         os.path.join(a.workdir, "m%d_%d.png" % (pi["idx"], dpi)), w)
            g = img.convert("L")
            bits = g.point(lambda v: 255 if v >= 190 else 0).convert("1")
            renders[key] = (bits, g)
        out = assemble(pdf, pageinfo, renders, dpi, at, arts, ovs)
        print("  dpi=%d art=%s -> %d bytes (%.0f KB)" % (dpi, at["art"], len(out), len(out) / 1024))
        if best is None or len(out) < len(best[0]):
            best = (out, at)
        if len(out) <= target:
            best = (out, at)
            break

    out, at = best
    if len(out) > target:
        print("WARNING: smallest layered build is %d bytes (%.0f KB) > target %d;"
              " falling back to per-page JPEG" % (len(out), len(out) / 1024, a.target_kb))
        out = fallback_jpeg(pdf, pageinfo, a, target)

    open(a.out, "wb").write(out)
    # trust but verify: the layered rebuild must still look like the source page
    try:
        d = page_diff(a.renderer, a.src, a.out, 0, a.workdir)
        print("  visual diff vs source: %.1f/255" % d)
        if d > 12:
            print("  layered build does not match the source -> raster fallback")
            out = fallback_jpeg(pdf, pageinfo, a, target)
            open(a.out, "wb").write(out)
    except Exception as e:
        print("  verification skipped:", e)
    print("wrote %s  %d bytes = %.1f KB (target <= %d KB)" %
          (a.out, len(out), len(out) / 1024, a.target_kb))
    return 0


def assemble(pdf, pageinfo, renders, dpi, at, arts, ovs):
    objects = []
    n_pages = len(pageinfo)
    # ids: 1 catalog, 2 pages, then per page: page, content
    page_ids = []
    oid = 3
    for pi in pageinfo:
        page_ids.append((pi, oid, oid + 1))
        oid += 2
    next_id = oid
    per_page_objs = {}
    for pi, pid, cid in page_ids:
        parts = [b"q 1 1 1 rg 0 0 %g %g re f Q" % (pi["media"][2], pi["media"][3])]
        xo = {}
        if pi["bg"] and at["art"]:
            num = pi["bg"][1]
            im = arts.get(num)
            if im is not None:
                comp, pal, w, h = encode_art(im, at["art_keep"], at["art_colors"],
                                                   dpi=dpi, q=at["q"], page_w=pi["media"][2])
                if pal is None:
                    objects.append(image_obj(next_id, b"/Type /XObject /Subtype /Image /Width %d /Height %d "
                                            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode"
                                            % (w, h), comp))
                else:
                    objects.append(image_obj(next_id, b"/Type /XObject /Subtype /Image /Width %d /Height %d "
                                            b"/ColorSpace [/Indexed /DeviceRGB %d <" % (w, h, len(pal) // 3 - 1)
                                            + bytes(pal).hex().encode() + b">] /BitsPerComponent 8 "
                                            b"/Filter /FlateDecode /DecodeParms << /Predictor 15 /Colors 1 "
                                            b"/BitsPerComponent 8 /Columns %d >>" % w, comp))
                xo["BG"] = next_id
                next_id += 1
                r = pi["bg"][2]
                parts.append(b"q %g 0 0 %g %g %g cm /BG Do Q" % (r[2], r[3], r[0], r[1]))
        bits, _ = renders[(pi["idx"], dpi)]
        if ImageChops.invert(bits).getbbox() is None:  # blank page, nothing to paint
            cz = zlib.compress(b"\n".join(parts), 9)
            objects.append((cid, b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(cz)
                            + cz + b"\nendstream"))
            per_page_objs[pid] = (pi, cid, xo)
            continue
        g4 = g4_of(bits)
        objects.append(image_obj(next_id, b"/Type /XObject /Subtype /Image /ImageMask true "
                                b"/Width %d /Height %d /Filter /CCITTFaxDecode /DecodeParms << /K -1 "
                                b"/Columns %d /Rows %d /BlackIs1 true >>" % (bits.width, bits.height,
                                                                             bits.width, bits.height), g4))
        xo["MK"] = next_id
        next_id += 1
        parts.append(b"q 0 g %g 0 0 %g 0 0 cm /MK Do Q" % (pi["media"][2], pi["media"][3]))
        for name, num, r in pi["overlays"]:
            im = ovs.get(num)
            if im is None:
                continue
            tw = max(24, int(round(r[2] / 72.0 * 200)))
            th = max(24, int(round(r[3] / 72.0 * 200)))
            im2 = im
            alpha = ALPHAS.get(num)
            if alpha is not None:
                # keep the transparency: paste onto the artwork beneath it, not onto white
                under = artwork_patch(pi, arts, r, (tw, th))
                if under is not None:
                    a = alpha.resize((tw, th), Image.LANCZOS)
                    under.paste(im.convert("RGB").resize((tw, th), Image.LANCZOS), (0, 0), a)
                    im2 = under
                else:
                    im2 = im
            cs, data, ow, oh = encode_overlay(im2, tw, th, at["q"])
            objects.append(image_obj(next_id, b"/Type /XObject /Subtype /Image /Width %d /Height %d "
                                    % (ow, oh) + cs, data))
            key = "OV%d" % next_id
            xo[key] = next_id
            next_id += 1
            parts.append(b"q %g 0 0 %g %g %g cm /%s Do Q" % (r[2], r[3], r[0], r[1], key.encode()))
        content = b"\n".join(parts)
        cz = zlib.compress(content, 9)
        per_page_objs[pid] = (pi, cid, xo)
        objects.append((cid, b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(cz) + cz + b"\nendstream"))

    for pid, (pi, cid, xo) in per_page_objs.items():
        xd = b" ".join(b"/%s %d 0 R" % (k.encode(), v) for k, v in xo.items())
        objects.append((pid, b"<< /Type /Page /Parent 2 0 R /MediaBox [%g %g %g %g] "
                             b"/Resources << /XObject << %s >> >> /Contents %d 0 R >>"
                        % (pi["media"][0], pi["media"][1],
                           pi["media"][0] + pi["media"][2], pi["media"][1] + pi["media"][3],
                           xd, cid)))
    objects.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objects.append((2, b"<< /Type /Pages /Kids [%s] /Count %d >>"
                    % (b" ".join(b"%d 0 R" % p for _, p, _ in page_ids), len(page_ids))))
    info_id = next_id
    objects.append((info_id, b"<< /Producer (pdf_shrink) /Creator (pdf_shrink) >>"))
    return build_pdf(sorted(objects), 1, info_id)


def artwork_patch(pi, arts, rect, size):
    """crop the page artwork under a rectangle, so overlays with alpha blend into the pattern"""
    if not pi["bg"]:
        return None
    art = arts.get(pi["bg"][1])
    if art is None:
        return None
    art = art.convert("RGB")
    pw, ph = pi["media"][2], pi["media"][3]
    sx, sy = art.width / float(pw), art.height / float(ph)
    x0 = int(rect[0] * sx)
    x1 = int((rect[0] + rect[2]) * sx)
    y1 = int(art.height - rect[1] * sy)
    y0 = int(art.height - (rect[1] + rect[3]) * sy)
    box = (max(0, x0), max(0, y0), min(art.width, x1), min(art.height, y1))
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return art.crop(box).resize(size, Image.LANCZOS)


def apply_smask(pdf, num, d, im):
    """remember a soft mask (/SMask n 0 R); compositing happens later over the artwork"""
    m = re.search(rb"/SMask\s+(\d+) 0 R", d)
    if not m:
        return im
    try:
        sm = decode_image(pdf, int(m.group(1))).convert("L")
    except Exception:
        return im
    if sm.size != im.size:
        sm = sm.resize(im.size, Image.LANCZOS)
    ALPHAS[num] = sm
    return im


def encode_overlay(im, tw, th, q):
    """QR codes / line art -> 1-bit Flate, photos -> JPEG"""
    small = im.convert("RGB").resize((tw, th), Image.LANCZOS)
    g = small.convert("L")
    hist = g.histogram()
    tot = float(tw * th)
    dark = sum(hist[:64]) / tot
    light = sum(hist[192:]) / tot
    if dark > 0.10 and light > 0.10 and (dark + light) > 0.75:
        b = g.point(lambda v: 255 if v >= 128 else 0).convert("1")
        data = zlib.compress(b.tobytes(), 9)
        return (b"/ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /FlateDecode", data, b.width, b.height)
    jb = io.BytesIO()
    small.save(jb, "JPEG", quality=q, optimize=True, subsampling=1)
    return (b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode",
            jb.getvalue(), tw, th)


def colour_count(im):
    """distinct colours, without interpolation (resampling invents new ones)"""
    if im.mode == "P":
        pal = im.getpalette()
        used = im.getcolors(1 << 16) or []
        return len({bytes(pal[i * 3:i * 3 + 3]) for _, i in used})
    th = im.convert("RGB").resize((64, 64), Image.NEAREST)
    return len(th.getcolors(1 << 20) or [])


def encode_art(im, keep_full, colors, dpi=None, q=80, page_w=595.0):
    """colour-limited artwork -> palette + PNG predictor; a photo/scan -> JPEG"""
    if colour_count(im) > 48:
        rgb = im.convert("RGB")
        want = int(round((dpi or 300) * page_w / 72.0))
        want = max(240, min(want, rgb.width))
        if rgb.width != want:
            rgb = rgb.resize((want, max(1, int(round(rgb.height * want / float(rgb.width))))),
                             Image.LANCZOS)
        b = io.BytesIO()
        rgb.save(b, "JPEG", quality=q, optimize=True, subsampling=1)
        return b.getvalue(), None, rgb.width, rgb.height
    if im.mode != "P":
        small = im.convert("RGB")
        if small.width > 1201:
            f = 1201.0 / small.width
            small = small.resize((1201, int(small.height * f)), Image.LANCZOS)
        p = small.convert("P", palette=Image.ADAPTIVE, colors=256)
    else:
        p = im
    pal = p.getpalette()
    counts = {}
    for cnt, oi in p.getcolors(1 << 16):
        rgb = bytes(pal[oi * 3:oi * 3 + 3])
        counts[rgb] = counts.get(rgb, 0) + cnt
    # ordering by frequency gives the best PNG predictor hits
    rgb_list = [r for r, _ in sorted(counts.items(), key=lambda kv: -kv[1])]
    distinct = {r: 0 for r in rgb_list}
    if colors and len(rgb_list) > colors:
        p = p.convert("RGB").quantize(colors=colors, method=Image.MEDIANCUT, dither=Image.NONE)
        pal = p.getpalette()
        counts = {}
        for cnt, oi in p.getcolors(1 << 16):
            rgb = bytes(pal[oi * 3:oi * 3 + 3])
            counts[rgb] = counts.get(rgb, 0) + cnt
        rgb_list = [r for r, _ in sorted(counts.items(), key=lambda kv: -kv[1])]
        distinct = {r: 0 for r in rgb_list}
    pal_out = bytearray()
    index_of = {}
    for i, rgb in enumerate(rgb_list):
        index_of[rgb] = i
        pal_out += rgb
    lut = bytearray(256)
    pal = p.getpalette()
    for oi in range(256):
        rgb = bytes(pal[oi * 3:oi * 3 + 3])
        if rgb in index_of:
            lut[oi] = index_of[rgb]
    px = bytes(lut[b] for b in p.tobytes())
    fr = Image.frombytes("P", p.size, px)
    fr.putpalette(bytes(pal_out))
    buf = io.BytesIO()
    fr.save(buf, "PNG", optimize=True, bits=8)
    png = buf.getvalue()
    idat = bytearray()
    pos = 8
    while pos < len(png):
        ln = int.from_bytes(png[pos:pos + 4], "big")
        typ = png[pos + 4:pos + 8]
        if typ == b"IDAT":
            idat += png[pos + 8:pos + 8 + ln]
        pos += 12 + ln
    return bytes(idat), pal_out, fr.width, fr.height


def page_diff(ps, pdf_a, pdf_b, page, workdir, width=320, step=7):
    """mean absolute luminance difference between the same page of two PDFs (0..255)"""
    ia = render(ps, pdf_a, page, os.path.join(workdir, "cmp_a_%d.png" % page), width).convert("L")
    ib = render(ps, pdf_b, page, os.path.join(workdir, "cmp_b_%d.png" % page), width).convert("L")
    if ia.size != ib.size:
        ib = ib.resize(ia.size, Image.LANCZOS)
    da, db = ia.tobytes()[::step], ib.tobytes()[::step]
    return sum(abs(x - y) for x, y in zip(da, db)) / float(max(1, len(da)))


def _is_gray(im, samples=4000):
    px = im.resize((100, 100)).getdata()
    diff = 0
    n = 0
    for r, g, b in list(px)[:samples]:
        diff += max(r, g, b) - min(r, g, b)
        n += 1
    return diff / max(1, n) < 4.0


def fallback_jpeg(pdf, pageinfo, a, target):
    """last resort: one flat JPEG per page (works on any PDF the renderer can open)"""
    pages = list(pageinfo)
    if not pages:
        pages = probe_pages(a)
        if not pages:
            raise RuntimeError("could not parse the page tree and the renderer opened no page")
    best = None
    # prefer resolution over colour fidelity: text stays readable at lower JPEG quality
    for dpi, quality in ((220, 40), (200, 45), (180, 45), (170, 50), (170, 70),
                         (150, 52), (150, 70), (130, 58), (110, 65), (96, 70)):
        objects = []
        page_ids = []
        oid = 3
        for pi in pages:
            img = render(a.renderer, a.src, pi["idx"],
                         os.path.join(a.workdir, "f%d_%d.png" % (pi["idx"], dpi)), dpi * 8.2677)
            rgb = img.convert("RGB")
            # colour pages cost ~3x the bytes: keep colour, but flatten near-grey pages
            is_gray = _is_gray(rgb)
            data_img = rgb.convert("L") if is_gray else rgb
            jb = io.BytesIO()
            data_img.save(jb, "JPEG", quality=quality, optimize=True, subsampling=1)
            cs = b"/ColorSpace /DeviceGray" if is_gray else b"/ColorSpace /DeviceRGB"
            objects.append(image_obj(oid + 1, b"/Type /XObject /Subtype /Image /Width %d /Height %d "
                                     % data_img.size + cs +
                                     b" /BitsPerComponent 8 /Filter /DCTDecode", jb.getvalue()))
            media = pi.get("media")
            if media is None:
                w = 595.0
                h = 595.0 * rgb.size[1] / float(rgb.size[0])
                media = (0.0, 0.0, w, h)
            c = b"q %g 0 0 %g 0 0 cm /I Do Q" % (media[2], media[3])
            cz = zlib.compress(c, 9)
            objects.append((oid + 2, b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(cz)
                            + cz + b"\nendstream"))
            objects.append((oid, b"<< /Type /Page /Parent 2 0 R /MediaBox [%g %g %g %g] "
                                 b"/Resources << /XObject << /I %d 0 R >> >> /Contents %d 0 R >>"
                            % (media[0], media[1], media[0] + media[2], media[1] + media[3],
                               oid + 1, oid + 2)))
            page_ids.append(oid)
            oid += 3
        objects.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
        objects.append((2, b"<< /Type /Pages /Kids [%s] /Count %d >>"
                        % (b" ".join(b"%d 0 R" % p for p in page_ids), len(page_ids))))
        info_id = oid
        objects.append((info_id, b"<< /Producer (pdf_shrink) >>"))
        out = build_pdf(sorted(objects), 1, info_id)
        print("  fallback dpi=%d q=%d -> %.0f KB" % (dpi, quality, len(out) / 1024))
        best = out
        if len(out) <= target:
            break
    return best


def probe_pages(a):
    """discover page count by rendering until the renderer refuses"""
    found = []
    for idx in range(40):
        png = os.path.join(a.workdir, "probe%d.png" % idx)
        try:
            im = render(a.renderer, a.src, idx, png, 200)
        except Exception:
            break
        found.append(dict(idx=idx, media=None, bg=None, overlays=[]))
        im.close()
    print("probed pages:", len(found))
    return found


if __name__ == "__main__":
    sys.exit(main())
