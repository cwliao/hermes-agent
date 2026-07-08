---
name: smart-ocr
description: "CJK document OCR with vertical-RTL column reading order."
version: 0.1.0
author: cwliao, Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [OCR, PaddleOCR, CJK, Chinese, Taiwan, Newspaper]
    related_skills: [ocr-and-documents]
---

# Smart OCR — High-quality CJK/RTL newspaper and document extraction

Specialized skill for **CJK document and newspaper scanning** where other
OCR produces garbled output because of column-misordering.

## When to Use

Use when the user needs OCR on a scanned image or PDF that contains Chinese,
Japanese (kanji+kana), or any RTL/CJK mixed content:

- Newspaper scans with right-to-left vertical columns (traditional Taiwan
  newspapers, books in vertical writing mode — `vertical-rl`)
- PDFs/scans where existing pipelines return nonsense like `研陳長三`
  concatenated across column boundaries instead of preserved paragraph-per-column structure
- Telegram images sent to hermes that need correct structured text output

Do NOT use this skill when the document contains no vertical CJK; default to
`ocr-and-documents` → pymupdf first.

## Prerequisites

Smart OCR requires **PaddleOCR** (CPU-only build, ~600 MB) plus Pillow and
OpenCV for image preprocessing; `pypdfium2` for PDF rendering:

```bash
pip install paddlepaddle pillow opencv-python-headless pypdfium2
hermes smartocr verify         # run to sanity-test installation (no input image required)
```

PaddleOCR ships its own lightweight detection/recognition modules and does
not depend on Tesseract. On first run the helper downloads the `ch_PP-OCRv3`
model (~65 MB) automatically to the PaddleOCR cache. Users with
slow networks can pre-stage this model by running
`hermes smartocr fetch-models --dir /tmp/paddle-models` on a networked host
and copying that directory to the offline host's PaddleOCR cache.

## How to Run (CLI)

The helper resolves layout automatically — traditional vertical CJK,
right-to-left newspapers, mixed-language documents with embedded English:

```bash
# Single file — auto-detects layout direction (LTR vs RTL column-first)
hermes smartocr ocr scanned-chinese.jpg
hermes smartocr ocr /path/to/newspaper.pdf

# Control preprocessing: auto (default), on, or off
hermes smartocr ocr ./scan.jpg                    # auto — decide per-image
hermes smartocr ocr ./scan.jpg --preprocess on    # force all steps (deskew + binarize + denoise)
hermes smartocr ocr ./scan.jpg --preprocess off   # skip all preprocessing (raw image)
hermes smartocr ocr ./scan.jpg --no-preprocess    # shorthand for --preprocess off

# Pick the language model (PaddleOCR code; default ch)
hermes smartocr ocr ./scan.jpg --lang ch          # Simplified Chinese
hermes smartocr ocr ./scan.jpg --lang chinese_cht # Traditional Chinese
hermes smartocr ocr ./scan.jpg --lang japan       # Japanese
hermes smartocr ocr ./scan.jpg --lang korean      # Korean
hermes smartocr ocr ./scan.jpg --lang en          # English (no RTL column sort)

# Retry on low quality: re-run with opposite preprocessing if OCR is poor
hermes smartocr ocr ./scan.jpg --retry            # auto-retry if chars < 5 or median confidence < 0.6
hermes smartocr ocr ./scan.jpg --preprocess off --retry  # try raw first; fall back to full pipeline

# Output format
hermes smartocr ocr ./scan.jpg --format text      # human-readable paragraphs
hermes smartocr ocr ./scan.jpg --format json      # one JSON object per line (good for piping / audit logs)

# Parallel batch
hermes smartocr ocr /path/to/scans/*.jpg --jobs 4        # 4 worker threads
hermes smartocr ocr /path/to/scans/*.jpg --jobs 1        # sequential (default 0 = auto = min(cpu_count, 4))
```

## Garbage Collection (`hermes smartocr gc`)

The GC prunes uploaded media so disk usage stays bounded. It scans
`~/.hermes/media/uploads/` and `/tmp/hermes-media` (PDF + image files only,
other extensions ignored):

```bash
# Dry-run — show what would be removed (default behaviour, never destructive)
hermes smartocr gc --scan
hermes smartocr gc                 # same as --scan (dry-run is the default)

# Actually delete
hermes smartocr gc --remove                # delete files older than 90 days (default)
hermes smartocr gc --remove --age 30       # delete files older than 30 days
hermes smartocr gc --remove --age 180      # delete files older than 180 days
```

For periodic cleanup, schedule `hermes smartocr gc --remove` from `cron` or
the built-in `cron` tool with the schedule you want (e.g. daily, weekly).

## Pitfalls

1. **PaddleOCR defaults to horizontal LTR** even if input has right-to-left
   columns; always verify output looks like proper paragraph structure when
   processing CJK newspaper scans — you should see one logical section per
   column (right-hand first), not a concatenated blob across every line.
2. Smart OCR returns **raw extraction only**. PaddleOCR is accurate but does
   not rewrite text to make it intelligible: long punctuation, broken word
   boundaries and weird whitespace remain after OCR. If you want clean output
   for downstream use (e.g., copy-paste), ask the assistant to "proofread and
   format" the result — it should already be structurally correct now thanks to
   RTL handling in SmartOCR's layout-aware post-process step.

## Verification

Test Smart OCR works on your system before relying on it in production:

1. Run `hermes smartocr verify` from anywhere — it checks the helper is
   loadable and that `paddleocr`, `pillow`, `opencv-python-headless` and
   (optionally) `pypdfium2` are importable.
2. Pass a sample image through the helper directly:
   `python optional-skills/productivity/smart-ocr/scripts/paddle_ocr_helper.py ocr /path/to/known-image.png`.
   Expected output should preserve column breaks when RTL layout is
   detected, and merge text as horizontal paragraphs otherwise.

## Notes

- Smart OCR uses PaddleOCR — the 92%+ accuracy CJK OCR framework from
  **Baidu** (Chinese-first), better for traditional Chinese/Taiwanese
  materials than Tesseract's `chi_tra` model for newspaper scans with
  complex vertical layout.
- The helper does NOT call an LLM; it only performs character recognition. To
  fix remaining noise or formatting after OCR, the user must ask for a second
  pass via hermes chat (e.g., "proofread this"); SmartOCR's job is to make that
  second pass possible by producing something intelligible in the first place.
- Tests at `tests/skills/test_smart_ocr_skill.py` cover RTL column-first
  reading order, horizontal layout path, and PaddleOCR install detection path;
  run with `scripts/run_tests.sh tests/skills/test_smart_ocr_skill.py -q`.
- SmartOCR is opt-in (optional skill); not part of base toolsets so it **does
  NOT** show up in hermes' system_prompt by default. Only enable when needed:
  set `skills.config.productivity.smart_ocr.enabled = true` under the active
  profile if you want to use Smart OCR regularly. After enabling, run a quick
  sanity test with hermes before using it on real content — otherwise the LLM
  might still route images through default OCR unless you reconfirm that in
  future sessions after `hermes reload` or similar restarts occur across profile boundaries.
