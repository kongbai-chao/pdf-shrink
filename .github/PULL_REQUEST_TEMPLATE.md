## What changed

<!-- one behaviour change per PR, please -->

## Command used and its output

```shell
$ python pdf_shrink.py tests/out/sample.pdf tests/out/shrunk.pdf --target-kb 200
```

```
<paste the dpi / visual diff / byte lines>
```

## Numbers

| | before | after |
|---|---|---|
| synthetic sample | | |
| a vector PDF you generated | | |

## Checklist

- [ ] No real document (or screenshot of one) is included
- [ ] `python -m py_compile pdf_shrink.py pdf_to_image.py` passes
- [ ] `visual diff vs source` is still under 12
- [ ] Only stdlib + Pillow added as a dependency
