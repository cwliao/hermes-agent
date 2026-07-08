# Smart OCR — Next Steps (project root TODO)

Mirror of `optional-skills/productivity/smart-ocr/TODO.md` at the project
root so it's discoverable when returning to this checkout. Last updated
2026-07-08. All 58/58 tests pass on numpy 2.x + cv2 4.x; branch in sync
with `origin/main` at HEAD `8665039fb0`.

## Done in this branch (all pushed)

- [x] Helper script (`paddle_ocr_helper.py`) — OCR engine, layout detection, preprocessing, quality retry, GC, parallel batch
- [x] SKILL.md — 58-char description, modern section order, all examples match real CLI
- [x] CLI subcommand parser + handler + `hermes_cli/main.py` integration
- [x] `INVENTORY.md` + per-skill `TODO.md` with smoke test transcript
- [x] **Live PaddleOCR smoke test** on a synthetic 1200x800 CJK newspaper image (Noto Serif CJK SC, two columns of 4 lines)
- [x] **Bug fix: `sort_rtl_columns` 10-px bucket boundary** (commit a6859d8947) — gap-based clustering replaces the broken `int(round(cx // 10)) * 10` bucket key
- [x] **Bug fix: margin-aware column detection + numpy 2.x compat** (commit 8665039fb0) — dynamic content extent from binary projection, P5 threshold instead of P30, `cv2.filter2D` output flattened to 1-D, near-binary detection dominates binarize decision
- [x] Two regression tests for the sort_rtl fix + 58 total tests passing

## Remaining follow-ups (in priority order)

### High value

1. **CLI integration tests** (`tests/skills/test_smart_ocr_skill.py` or new file)
   - subprocess-level tests for `hermes_cli/smartocr.py:run_smartocr()`
   - Mock `subprocess.run` to assert the right argv is passed to `paddle_ocr_helper.py` for each subcommand
   - Currently only the helper is unit-tested; the CLI wrapper has zero coverage
   - Closes a real gap in the test pyramid

2. **CJK gate on `detect_columns_image`**
   - Currently runs unconditionally
   - When `lang not in _CJK_LANGS`, skip image-level column-gap detection entirely
   - Saves ~30% per-page latency in English-only workflows
   - Also avoids the OTSU + projection computation overhead

3. **English fast-path short-circuit**
   - When `lang == "en"`, skip preprocessing AND image-level column detection
   - English documents are always LTR; no RTL reordering, no column-gap analysis
   - Should be a single early-return in `ocr_image` / `ocr_pdf`

### Medium value

4. **Multi-profile test**
   - Confirm GC scans profile-aware `$HERMES_HOME/media/uploads` for a non-default profile
   - Use the `profile_env` pattern from `tests/hermes_cli/test_profiles.py`
   - Verifies the `os.environ.get("HERMES_HOME", ...)` fallback in the helper

5. **Windows platform broadening**
   - Audit passes (no POSIX-only primitives: no `fcntl`, no `os.setsid`, no `/proc`, no `osascript`)
   - PaddlePaddle ships Windows wheels, pypdfium2 and opencv-python-headless are cross-platform
   - Update `SKILL.md` `platforms:` to `[linux, macos, windows]`
   - Optional: run a smoke test on Windows to confirm

### Low value / out of scope

6. CLI: `--page-range` for multi-page PDFs
7. CLI: `--progress` bar for parallel batches
8. CJK whitelist for column-gap detection (similar to #2)
9. Integration with `delegate_task` (would need a new core tool, Footprint Ladder rung 6)
10. Real-world CJK newspaper image (current smoke test uses synthetic image)

## Verification commands (re-runnable)

```bash
cd /home/cwliao/.hermes/hermes-agent
source venv/bin/activate

# Unit tests (CI-parity, 58/58 expected)
scripts/run_tests.sh tests/skills/test_smart_ocr_skill.py -q

# OCR smoke (synthetic image from last session may still be at /tmp/opencode/scans/
# after a reboot, regenerate via the script in optional-skills/.../TODO.md "Smoke test results")
hermes smartocr verify
python optional-skills/productivity/smart-ocr/scripts/paddle_ocr_helper.py \
    ocr /tmp/opencode/scans/cjk-newspaper.png --lang ch

# CLI registration smoke
python -m hermes_cli.main smartocr --help
python -m hermes_cli.main smartocr ocr --help
python -m hermes_cli.main smartocr gc --help
```

## Re-bootstrapping the paddlepaddle stack

```bash
pip install paddlepaddle pypdfium2 paddleocr
pip install 'pyyaml==6.0.3'   # resolves paddlex 6.0.2 vs hermes-agent 6.0.3 conflict
hermes smartocr verify
```

## Open questions / decisions for the user

- **pyyaml conflict** between paddlex (wants 6.0.2) and hermes-agent (wants 6.0.3). Currently non-fatal. Worth filing an upstream issue with paddlex, or a follow-up to relax hermes-agent's pin.
- **Windows broadening** — should it be `[linux, macos, windows]` or stay conservative at `[linux, macos]` until a Windows run is done?
- **CJK gate scope** — should the gate affect only `detect_columns_image`, or also the bbox-based `detect_rtl_layout` in `_process_raw`? (The latter is already gated on `lang in _CJK_LANGS`.)
