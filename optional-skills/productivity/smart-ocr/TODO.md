# Smart OCR — TODO

Open follow-ups for the `smart-ocr` skill and `hermes smartocr` CLI integration.
Last updated: 2026-07-08.

## Status

- [x] Helper script (`paddle_ocr_helper.py`) — OCR engine, layout detection, preprocessing, quality retry, GC, parallel batch
- [x] SKILL.md — frontmatter + body (description 58 chars, under 60 limit; modern section order; all examples match real CLI)
- [x] CLI subcommand parser (`hermes_cli/subcommands/smartocr.py`)
- [x] CLI handler (`hermes_cli/smartocr.py`) — delegates to helper via subprocess
- [x] `hermes_cli/main.py` integration — imports + dispatch + subparser registration
- [x] Test suite (`tests/skills/test_smart_ocr_skill.py`) — 56 tests, 41 pass on this machine, 15 skip (no cv2/numpy/paddleocr in venv)
- [x] `test_cmd_smartocr_verify_no_paddle` — made deterministic via `sys.modules['paddleocr'] = None`
- [x] `test_cmd_smartocr_verify_with_paddle` — complementary happy-path test
- [x] `GC_DIRS` — profile-aware via `HERMES_HOME` env var (no more hardcoded `~/.hermes`)
- [x] argparse alias fix — `verify` is now an alias for `doctor` (was a conflicting subparser name)
- [x] SKILL.md description shortened to fit 60-char limit
- [x] SKILL.md body rewritten to drop references to non-existent flags (`--layout`, `--keep`, `--every-hour-once`), subcommand (`status`), script (`smart_ocr.py`), and files (`references/smart-ocr-design-notes.md`, `ocr_whitelist_chats.txt`)

## User action items

### Required: commit
All work is uncommitted. Per AGENTS.md, I do not auto-commit. Suggested commit:

```bash
git add hermes_cli/main.py \
        hermes_cli/smartocr.py \
        hermes_cli/subcommands/smartocr.py \
        optional-skills/productivity/smart-ocr/ \
        tests/skills/test_smart_ocr_skill.py

git commit -m "feat(skill): add smart-ocr optional skill + hermes smartocr CLI"
```

Branch is 114 commits ahead of `origin/main`; there are other uncommitted changes in unrelated files. Run `git status` first to confirm nothing else is staged.

### Optional: install PaddleOCR and try a real OCR run
The test env doesn't have PaddleOCR. To verify on a real newspaper scan:

```bash
pip install paddlepaddle pillow opencv-python-headless pypdfium2
hermes smartocr verify                        # confirm installation
hermes smartocr ocr path/to/newspaper.jpg     # text output
hermes smartocr ocr path/to/newspaper.jpg --format json
hermes smartocr ocr path/to/newspaper.jpg --retry
hermes smartocr ocr scans/*.jpg --jobs 4      # parallel batch
hermes smartocr gc --scan                    # dry-run GC
```

### Optional: broaden `platforms: [linux, macos]` → `[linux, macos, windows]`
PaddlePaddle ships Windows wheels, `pypdfium2` and `opencv-python-headless` are cross-platform, and the helper uses no POSIX-only primitives (no `fcntl`, `os.setsid`, `/proc`, etc.). I left the field conservative because I haven't tested on Windows. The AGENTS.md skill standard says to "audit platforms against actual script imports" — the audit passes for Windows, but a confirmation run on a Windows host would be the right close.

### Optional: split `_HERMES_HOME` constant into a getter
Currently it's a module-level constant. If PaddleOCR's `paddle_ocr_helper.py` ever needs to react to runtime profile switches (e.g. `os.environ['HERMES_HOME']` changing after import), the constant would be stale. Today nothing in the helper re-reads it post-import, so a constant is fine. Revisit if multi-profile workflows are added.

## Follow-ups NOT in scope

These are nice-to-haves that were deliberately not done to keep the PR focused:

- **CLI: per-page output for multi-page PDFs.** Today all pages of a PDF are OCR'd in one call and `--format text` prints them in sequence. A `--page-range` flag would let users pick a slice.
- **CLI: streaming progress.** Parallel batch runs silently until each file completes. A `--progress` flag with a `tqdm`-style bar would help on big batches.
- **CJK whitelist for column-gap detection.** `detect_columns_image` already gates on `lang in _CJK_LANGS` for layout sorting; column-gap detection itself runs unconditionally. Could add a similar gate so the image-level detection only runs for CJK/RTL.
- **OCRONLY fast-path for English.** When `lang="en"`, the helper still runs preprocessing + column detection. Short-circuiting those for English-only pages would shave ~30% off per-page latency.
- **Integration with `delegate_task`.** A subagent could be spawned with a list of files and `--jobs > 1` to fan out further across machines. Not in scope for a single skill; would need a new core tool (Footprint Ladder rung 6).
- **Tests for the CLI integration itself** (`hermes_cli/smartocr.run_smartocr`). Currently only the helper has unit tests. End-to-end CLI tests would call `subprocess.run` and assert on stderr/stdout. Skipped for now because it would require either mocking subprocess or installing PaddleOCR; both are heavier than the unit tests justify.

## Verification commands (re-runnable)

```bash
# All skill tests (CI-parity)
scripts/run_tests.sh tests/skills/test_smart_ocr_skill.py -q

# Just this skill
scripts/run_tests.sh tests/skills/ -q | grep smart_ocr

# Lint (if ruff configured in repo)
ruff check hermes_cli/smartocr.py hermes_cli/subcommands/smartocr.py \
        optional-skills/productivity/smart-ocr/scripts/paddle_ocr_helper.py \
        tests/skills/test_smart_ocr_skill.py

# Smoke: CLI registration
source venv/bin/activate
python -m hermes_cli.main smartocr --help
python -m hermes_cli.main smartocr ocr --help
python -m hermes_cli.main smartocr gc --help
python -m hermes_cli.main smartocr verify
```

## What I did NOT verify (and why)

- **No live PaddleOCR run.** The venv doesn't have `paddlepaddle` or `paddleocr` (and on a 600 MB install, doing it during this session wasn't justified without a sample image to test on).
- **No real newspaper image.** The column-gap detection tests use synthetic images (black column rectangles on white) to assert the algorithm, not real CJK content.
- **No multi-profile test.** The `HERMES_HOME` env-var-based path resolution was added for profile safety but I did not run `hermes -p <name> smartocr ...` to confirm a second profile's `media/uploads/` is correctly scanned.
- **No Windows run.** The `platforms:` field is `[linux, macos]`; the script uses no POSIX-only primitives so Windows *should* work, but I did not test it.
