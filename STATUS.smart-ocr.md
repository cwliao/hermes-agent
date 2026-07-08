# Smart OCR — Session Status

Snapshot of where the smart-ocr skill and `hermes smartocr` CLI integration
stand right now. Picked up from a prior session; safe to resume from here.

## Branch state

```
HEAD:   8665039fb0 fix(smart-ocr): margin-aware column detection + numpy 2.x compat
        a6859d8947 fix(smart-ocr): sort_rtl_columns scrambles RTL order at 10-px boundary
        2bafdcf1fe feat(skill): add smart-ocr optional skill + hermes smartocr CLI

Branch: main, in sync with origin/main (0 ahead)
Tree:   clean, no uncommitted changes
```

All three commits pushed to `github.com:cwliao/hermes-agent`.

## What works

| Surface | Status |
|---------|--------|
| `hermes smartocr ocr <image>` | working end-to-end on Linux + Python 3.11 + paddlepaddle 3.2.2 + paddleocr 3.7.0 + pypdfium2 |
| `hermes smartocr ocr <pdf>` | working (pypdfium2 renders each page, OCR per page) |
| `hermes smartocr ocr <glob> --jobs N` | parallel batch via ThreadPoolExecutor |
| `hermes smartocr gc --scan / --remove` | profile-aware via `$HERMES_HOME/media/uploads` + `/tmp/hermes-media` |
| `hermes smartocr fetch-models --dir <dst>` | pre-stages PaddleOCR models for offline transfer |
| `hermes smartocr doctor` / `verify` | checks paddleocr + pypdfium2 importability |
| RTL column reading order | **fixed** in 8665039fb0 — was scrambled for some preprocessed images |
| Margin-aware column detection | **fixed** in 8665039fb0 — single-column images with wide margins no longer mis-classify margins as column gaps |
| `tests/skills/test_smart_ocr_skill.py` | **58/58 pass** under `scripts/run_tests.sh` |

## Installed runtime deps (in venv)

- `paddlepaddle 3.2.2`
- `paddleocr 3.7.0`
- `paddlex 3.7.2` (transitive dep of paddleocr)
- `pypdfium2` (no `__version__` attr; installed alongside paddlepaddle)
- `cv2 4.10.0` (pulled in by paddlepaddle)
- `numpy 2.4.3`
- `pyyaml 6.0.3` (hermes-agent pins 6.0.3; paddlex requests 6.0.2 — pip emits a non-fatal resolver warning but installs 6.0.3 OK)

PaddleOCR model cache: `~/.paddlex/official_models/` (PaddleOCR's own cache, not
profile-aware because PaddleOCR owns it).

## CLI surface

```
hermes smartocr
├── ocr
│   paths (positional, nargs="+")
│   --format {text,json}         (default: text)
│   --preprocess {auto,on,off}   (default: auto)
│   --no-preprocess              (shorthand for --preprocess off)
│   --lang LANG                  (default: ch; supports ch, en, japan, korean, chinese_cht)
│   --retry                      (re-run with opposite preprocessing if quality is low)
│   --jobs N                     (parallel workers; 0=auto, 1=sequential)
├── gc
│   --scan                       (dry-run; default)
│   --remove                     (actually delete)
│   --age DAYS                   (default: 90)
├── fetch-models
│   --dir DIR                    (copy cached models to this directory)
└── doctor (alias: verify)       (check paddleocr + pypdfium2 imports)
```

## Files in this PR

```
hermes_cli/main.py                                         (modified, +13)
hermes_cli/smartocr.py                                     (new)
hermes_cli/subcommands/smartocr.py                         (new)
optional-skills/productivity/smart-ocr/INVENTORY.md        (new)
optional-skills/productivity/smart-ocr/SKILL.md            (new)
optional-skills/productivity/smart-ocr/TODO.md             (new, full smoke test results)
optional-skills/productivity/smart-ocr/scripts/paddle_ocr_helper.py  (new, 1060 lines)
tests/skills/test_smart_ocr_skill.py                       (new, 58 tests)
```

Per-skill `INVENTORY.md` and `TODO.md` are at
`optional-skills/productivity/smart-ocr/`. They contain the helper-script
entry-point map, full smoke test transcript, and the pre-fix / post-fix
reading-order outputs that surfaced the `sort_rtl_columns` bucket bug.

## What to re-run first to confirm state

```bash
cd /home/cwliao/.hermes/hermes-agent
git log --oneline -3          # confirm HEAD is 8665039fb0
git status                   # should be clean (only STATUS.smart-ocr.md + TODO.smart-ocr.md untracked before commit)

source venv/bin/activate
scripts/run_tests.sh tests/skills/test_smart_ocr_skill.py -q
# expected: 58 passed, 0 failed

hermes smartocr verify
# expected: OK Smart OCR helper is loadable. / OK PaddleOCR is installed.
#          OK pypdfium2 is installed (PDF support).
```

## Re-bootstrapping deps (fresh checkout)

The paddlepaddle stack is heavy (~700 MB). If a fresh venv doesn't have it:

```bash
source venv/bin/activate
pip install paddlepaddle pypdfium2 paddleocr
# if pip warns pyyaml conflict, force the hermes-agent pin:
pip install 'pyyaml==6.0.3'
hermes smartocr verify    # confirm install
```

The synthetic CJK test image (`/tmp/opencode/scans/cjk-newspaper.png`) lives
under `/tmp/opencode/` and may not survive a reboot. Regenerate with the
inline script at the bottom of this file's sibling `TODO.smart-ocr.md`
("Smoke test results" section) or just skip the OCR smoke and rely on the
58 unit tests, which don't need a real image.
