# Smart OCR — File Inventory

Snapshot of all files added or modified for the `smart-ocr` optional skill
and its `hermes smartocr` CLI integration. Last updated: 2026-07-08.

## New files

| Path | Lines | Purpose |
|------|-------|---------|
| `optional-skills/productivity/smart-ocr/SKILL.md` | 153 | Skill manifest (frontmatter + usage docs) |
| `optional-skills/productivity/smart-ocr/scripts/paddle_ocr_helper.py` | 1060 | OCR engine: PaddleOCR wrapper + RTL/CJK column detection + auto preprocessing + quality retry + media GC + parallel batch |
| `hermes_cli/smartocr.py` | 112 | `run_smartocr(args)` dispatch handler — bridges CLI args to helper script via subprocess |
| `hermes_cli/subcommands/smartocr.py` | 49 | `build_smartocr_parser()` — argparse subparser tree for `hermes smartocr` |
| `tests/skills/test_smart_ocr_skill.py` | 871 | 56 unit tests covering RTL detection, column sort, parse result, GC, image-level column-gap detection, font-height estimation, parallel batch, auto-preprocess, quality score, retry resolution, verify command |

## Modified files

| Path | Change | Purpose |
|------|--------|---------|
| `hermes_cli/main.py` | +13 lines | Imports `build_smartocr_parser`, defines `cmd_smartocr`, registers it after `build_doctor_parser` in `main()` |

## CLI surface added

```
hermes smartocr
├── ocr            Run OCR on one or more images or PDFs
│   ├── paths (positional, nargs="+")
│   ├── --format {text,json}         (default: text)
│   ├── --preprocess {auto,on,off}   (default: auto)
│   ├── --no-preprocess              (shorthand for --preprocess off)
│   ├── --lang LANG                  (default: ch; supports ch, en, japan, korean, chinese_cht)
│   ├── --retry                      (re-run with opposite preprocessing if quality is low)
│   └── --jobs N                     (parallel workers; 0=auto=min(cpu_count,4); 1=sequential)
│
├── gc             Media garbage collection
│   ├── --scan                       (dry-run; default)
│   ├── --remove                     (actually delete)
│   └── --age DAYS                   (default: 90)
│
├── fetch-models   Pre-stage PaddleOCR models for offline transfer
│   └── --dir DIR                    (copy cached models to this directory)
│
└── doctor (alias: verify)   Check Smart OCR environment (paddleocr / pypdfium2 imports)
```

## Helper script entry points

`optional-skills/productivity/smart-ocr/scripts/paddle_ocr_helper.py`:

| Function | Visibility | Purpose |
|----------|------------|---------|
| `main()` | CLI entry | Argparse + dispatch to ocr / gc / fetch-models |
| `ocr_image(path, ...)` | public | Run OCR on a single image; returns `{filename, layout, paragraphs, chars, preprocess}` |
| `ocr_pdf(path, ...)` | public | Run OCR on a PDF (multi-page); returns list of result dicts |
| `_ocr_file(path, ...)` | internal | Dispatch image vs PDF |
| `_parse_result(raw)` | internal | Normalise PaddleOCR output (v3.7 dict format + legacy list format) |
| `detect_rtl_layout(lines)` | public | Bbox tall/narrow ratio → RTL yes/no |
| `sort_rtl_columns(lines)` | public | Group by x, sort right→left, within column top→bottom |
| `_process_raw(raw, *, img_columns, lang)` | internal | Layered layout decision (image-gaps → bbox heuristic, gated on CJK) |
| `preprocess_image(img, ...)` | public | deskew → binarize → denoise pipeline |
| `deskew(img)` | public | Hough-line skew detection + rotation |
| `binarize_adaptive(img, ...)` | public | Adaptive Gaussian threshold |
| `denoise(img, strength)` | public | medianBlur (light) or fastNlMeansDenoisingColored (heavy) |
| `_auto_preprocess_decision(img)` | internal | Returns (deskew, binarize, denoise, contrast_cv, extremal_ratio) |
| `detect_columns_image(img)` | public | Vertical projection → list of (gap_start, gap_end) column gaps |
| `_estimate_font_height(binary)` | internal | Median connected-component height, filtered for noise |
| `_detect_columns_sigma(w, font_h)` | internal | Font-aware Gaussian sigma for column detection |
| `_quality_score(result)` | internal | Median line confidence (or fallback); −1.0 = garbage |
| `_resolve_opposite_preprocess(mode, auto_decided)` | internal | on↔off; auto+decided→opposite of decided |
| `_ocr_with_retry(ocr_fn, ...)` | internal | Wrapper that retries with opposite preprocessing on bad score |
| `_cmd_fetch_models(copy_dir)` | internal | Trigger dummy inference to populate PaddleOCR cache, then list/copy |
| `_resolve_jobs(n)` | internal | Clamp worker count to safe range |
| `_scan_media(age_days, dry_run)` | internal | GC: list (or delete) media files older than threshold |

## Test coverage (`tests/skills/test_smart_ocr_skill.py`)

56 test functions, 41 pass on this machine, 15 skip (skip = needs `cv2` / `numpy` / `paddleocr` deps not in this venv).

| Group | Tests | Notes |
|-------|-------|-------|
| RTL layout detection | 5 | `detect_rtl_layout`, `_tall_narrow_ratio`, threshold boundary |
| Column sort | 3 | `sort_rtl_columns` rightmost-first, top-down, empty |
| Parse result | 4 | dict / list / new v3.7+ format / empty entries |
| Confidence extraction | 4 | new format / dict / legacy / default-when-missing |
| GC `_scan_media` | 4 | finds old / deletes old / noop when fresh / skips non-image |
| Image-level column-gap detection | 5 | two-column / single / low-ink / margin-ignored / three-column |
| Font-height estimation | 3 | normal / blank / noise-filtered |
| Adaptive sigma | 2 | font-h-driven / width fallback |
| DPI scaling | 2 | high-DPI / low-DPI equivalent column detection |
| Image-layer overrides bbox heuristic | 2 | image-columns present / empty |
| CJK gating | 2 | non-CJK disables RTL / CJK enables RTL |
| Parallel batch | 2 | `_resolve_jobs` explicit / auto |
| Auto-preprocess decision | 5 | clean-skips / noisy-enables / always-deskews / denoise-tracks-binarize / returns heuristic values |
| Preprocess metadata in JSON output | 3 | JSON includes preprocess / JSON strips _lines / text ignores preprocess |
| Quality score | 5 | empty / no-lines-fallback / high-confidence / low-chars-low-conf → −1.0 / low-chars-high-conf OK |
| Opposite-preprocess resolution | 3 | on↔off / auto-uses-decided / auto-without-decided raises |
| Verify command | 2 | paddleocr missing → SystemExit(1) / paddleocr present → prints OK (both stubbed deterministically via `sys.modules`) |

## Module dependency chain (unchanged from AGENTS.md)

```
hermes_cli/subcommands/smartocr.py   (no deps beyond argparse)
        ↑
hermes_cli/smartocr.py              (subprocess → helper script)
        ↑
hermes_cli/main.py                  (imports the parser, registers it)
```

The helper script is **not** imported by core — it's invoked via `subprocess.run([sys.executable, HELPER, ...])`. This keeps `hermes-cli` importable even when PaddleOCR / OpenCV are missing, and lets the skill be installed standalone.

## Files NOT touched

- `toolsets.py` — no core tool surface added; `smartocr` is CLI-only by design (Footprint Ladder rung 2)
- `tools/*.py` — no new core tool
- `run_agent.py` — no model-tool change
- `~/.hermes/...` — no user config files modified

## State files the helper reads/writes (profile-aware)

- Reads: `$HERMES_HOME/media/uploads/`, `/tmp/hermes-media/` (via `GC_DIRS`)
- Reads: `~/.paddlex/official_models/` (PaddleOCR's own cache; not profile-aware because PaddleOCR owns it)
- Writes: stdout / stderr only (no hermes-side state)
