# Contributing

Thanks for looking. A few things worth knowing before you open a PR.

## Ground rules

- **Never commit a real document.** The repository ships a synthetic sample
  (`tests/make_sample.py`); screenshots, fixtures and issue attachments must be
  anonymised or generated. If a bug can only be reproduced with a specific
  publisher's PDF, describe the structure (object types, byte breakdown) instead
  of attaching the file.
- **Keep it dependency-light.** Standard library + Pillow only. No PDF toolchain
  (Ghostscript, poppler, MuPDF) — the whole point is that `Windows.Data.Pdf` is
  already on the machine.

## Development

```powershell
python -m pip install pillow
python tests/make_sample.py tests/out/sample.pdf
python pdf_shrink.py tests/out/sample.pdf tests/out/shrunk.pdf --target-kb 200
python pdf_to_image.py tests/out/sample.pdf tests/out/page --target-kb 200
```

Both scripts print `visual diff vs source: N/255`. Treat **> 12** as a failure:
it means the rebuild dropped a layer, and the tool should have fallen back.

## Where things live

| file | role |
|---|---|
| `pdf_shrink.py` | page classification, layer encoders, PDF writer, verification |
| `pdf_to_image.py` | dpi/quality search under a byte budget |
| `render_page.ps1` | the only platform-specific piece — one page to PNG |
| `docs/algorithm.md` | measurements behind the default parameters |

## The most useful contribution

**Portability.** Replacing `render_page.ps1` with something cross-platform
(`pdftoppm -r`, `mutool draw -o`, or `pypdfium2`) unblocks Linux and macOS, and
nothing else in the pipeline is Windows-specific. If you do it, keep the CLI
identical and gate it behind a `--renderer`-style switch.

## Pull requests

- One behaviour change per PR, with the command you ran and its output.
- If you change a threshold (dpi ladder, `visual diff` cut-off, the bilevel
  criterion in `encode_overlay`), include before/after numbers on the synthetic
  sample and on at least one vector PDF you generated yourself.
- `python -m py_compile` must pass on the files you touch.
