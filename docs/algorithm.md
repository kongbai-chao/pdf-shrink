# Algorithm notes — measurements, parameter choices, dead ends

Everything below was measured on real Chinese documents (online degree-verification
reports, university transcripts, a CNIPA utility-model certificate), 1–2 pages, A4,
produced by iText or similar report generators. No personal data from those files is
included in this repository.

## Where the bytes are (471 KB, one page)

| object | bytes | what it is |
|---|---|---|
| `CIDFontType0C` | 233,959 | Source Han Sans SC subset (`Identity-H`) |
| `/Indexed` background | 180,975 | anti-counterfeit border + CHSI watermark, 1200×1698, 13 colours |
| photo JPEG | 36,297 | 480×640 ID photo |
| QR JPEG | 13,629 | 147×147 |
| content stream | 1,307 | text |
| everything else | ~5,400 | logo, fonts dict, page, xref |

Conclusion: dropping images alone gets you to ≈306 KB. **The font has to go**, and
since it is keyed by `Identity-H` CIDs, subsetting it in place is not a weekend
project — so the text is rasterized instead.

## Why 400 dpi + CCITT G4 for the text layer

| encoding | size | notes |
|---|---|---|
| G4, 300 dpi | 41 KB | threshold 150 — thin strokes survive, faint texture does not |
| G4, 400 dpi | 55–101 KB | threshold 190; 400 dpi is the sweet spot |
| G4, 450 dpi | 55–173 KB | no visible gain over 400 |
| Flate, 1-bit, 400 dpi | 150–250 KB | G4 is 3–5× smaller |
| Grayscale JPEG, 300 dpi q50 | 563 KB | text is expensive in DCT |

At 400 dpi a 10 pt Chinese glyph is ~55 px tall — it prints cleanly.

## Background artwork: keep the pixels, fix the container

The artwork is line art with 13 distinct colours stored as a 256-entry indexed
image. Three encodings compared:

| encoding | size |
|---|---|
| original (iText Flate) | 181 KB |
| 16-colour JPEG q70, 1201 px | 410 KB |
| **palette-dedup + PNG predictor + Flate** | **152 KB** |
| same, downsampled to 700 px, JPEG q50 | 92 KB — but the lace pattern goes visibly darker |
| same, nearest-neighbour to 900 px | 117 KB — moiré in the lace |

Downsampling *lowers* the resolution of a 1-px line pattern but JPEG puts the
energy back as ringing, so the page reads darker than the original. Keeping the
native resolution and only changing the compression was both smaller and more
faithful.

**Pitfall that cost an hour:** the source image declares `/Width 1200` but its
stream is `1698 × (1200 + 1)` bytes — it uses `/Predictor 15`, so each row carries
a PNG filter byte. Reading it as 1201-px rows produced a diagonal skew that looked
like a design element until it was compared side by side.

## Overlays

- QR (147×147) as JPEG q78 at 188 px: 16.9 KB. As 1-bit Flate: **1.4 KB**, and it
  stays scannable. Always send bilevel content through a bilevel codec.
- Photo: 480×640 → 221×294 JPEG q78 ≈ 8 KB; more is invisible at print size.
- Logo with `/SMask`: compositing onto white leaves a visible white box over the
  watermark. Composite onto the *artwork crop under the logo rect* instead.

## Threshold choice

| threshold | effect |
|---|---|
| 150 | page texture gone, text slightly thin, clean white background |
| 190 | good default — antialiased stroke edges kept |
| 220–235 | keeps the decorative frame, but the frame becomes a heavy stipple |
| 245 | keeps the watermark, costs 2× the bytes |

## Dead ends

- **JPEG 2000 for the whole page.** `dB 30` gives 97 KB at 300 dpi and looks good,
  and PDF supports `/JPXDecode` — but pdf.js does not decode JPEG 2000, and plenty
  of portals preview uploads with pdf.js. Too risky for an official document.
- **Bilevel for the whole page.** 30–55 KB and razor sharp, but a pure
  black/white zone turns the anti-counterfeit frame into a harsh black band.
- **Grayscale everything.** Cheaper, but red seals and blue ID photos are the
  first thing a reviewer looks for.
- **Downsampling the artwork to hit the target.** It costs more fidelity per byte
  than any other lever; dropping the artwork layer entirely (the `art=False`
  rung) looks cleaner than a mushy version of it.

## Verification thresholds

`page_diff()` renders both files at 320 px wide and takes the mean absolute
luminance difference over a 1-in-7 sample:

- 2–4 → normal (antialiasing and texture rounding)
- > 12 → structurally wrong (missing layer, wrong page, wrong page count) → fallback

320 px is deliberate: big enough to notice a missing 60×40 pt logo or a shifted
photo, small enough to ignore JPEG noise.
