"""Unit tests for the Smart OCR helper — layout detection and GC."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


def _get_helper():
    """Load the helper module via importlib (avoids PaddleOCR at import time)."""
    import importlib.util as iu

    root = Path(__file__).resolve().parent.parent.parent
    path = (
        root
        / "optional-skills"
        / "productivity"
        / "smart-ocr"
        / "scripts"
        / "paddle_ocr_helper.py"
    )
    if not path.is_file():
        pytest.skip(f"Helper not found: {path}")
    spec = iu.spec_from_file_location("paddle_ocr_helper", str(path))
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    helper = _get_helper()
except (pytest.skip.Exception, Exception):
    helper = None


# ===========================================================================
# RTL layout detection
# ===========================================================================

_TALL = {"box": [(0, 0), (10, 0), (10, 60), (0, 60)], "text": "A"}
_WIDE = {"box": [(0, 0), (80, 0), (80, 20), (0, 20)], "text": "B"}


def test_detect_rtl_returns_true_when_majority_tall():
    if helper is None:
        pytest.skip("helper not available")
    lines = [_TALL] * 5 + [_WIDE] * 2
    assert helper.detect_rtl_layout(lines) is True


def test_detect_rtl_returns_false_when_majority_wide():
    if helper is None:
        pytest.skip("helper not available")
    lines = [_WIDE] * 5 + [_TALL] * 1
    assert helper.detect_rtl_layout(lines) is False


def test_detect_rtl_returns_false_on_empty():
    if helper is None:
        pytest.skip("helper not available")
    assert helper.detect_rtl_layout([]) is False


def test_rtl_threshold_boundary():
    if helper is None:
        pytest.skip("helper not available")
    # 40% tall → still False (needs > 0.4)
    lines = [_TALL] * 4 + [_WIDE] * 6
    assert helper.detect_rtl_layout(lines) is False


def test_tall_narrow_ratio_with_new_threshold():
    if helper is None:
        pytest.skip("helper not available")
    # vertical column: h > w*2 → counts as tall/narrow
    box = {"box": [(0, 0), (60, 0), (60, 200), (0, 200)], "text": "col"}
    assert helper._tall_narrow_ratio([box]) == 1.0


def test_tall_narrow_rejects_horizontal_text():
    if helper is None:
        pytest.skip("helper not available")
    # horizontal text: w >> h → not counted
    box = {"box": [(0, 0), (300, 0), (300, 30), (0, 30)], "text": "wide"}
    assert helper._tall_narrow_ratio([box]) == 0.0


# ===========================================================================
# Column sort
# ===========================================================================


def test_sort_rtl_columns_rightmost_first():
    if helper is None:
        pytest.skip("helper not available")
    # x=100 is physically rightmost on page → read first in RTL
    # x=30 is physically leftmost on page → read last in RTL
    leftmost = {"box": [(30, 0), (40, 0), (40, 40), (30, 40)], "text": "leftmost"}
    rightmost = {"box": [(100, 0), (110, 0), (110, 40), (100, 40)], "text": "rightmost"}
    sorted_ = helper.sort_rtl_columns([leftmost, rightmost])
    assert sorted_[0]["text"] == "rightmost"
    assert sorted_[1]["text"] == "leftmost"


def test_sort_rtl_columns_within_column_top_to_bottom():
    if helper is None:
        pytest.skip("helper not available")
    bottom = {"box": [(50, 60), (60, 60), (60, 100), (50, 100)], "text": "bottom"}
    top = {"box": [(50, 0), (60, 0), (60, 40), (50, 40)], "text": "top"}
    sorted_ = helper.sort_rtl_columns([bottom, top])
    assert sorted_[0]["text"] == "top"
    assert sorted_[1]["text"] == "bottom"


def test_sort_rtl_columns_empty():
    if helper is None:
        pytest.skip("helper not available")
    assert helper.sort_rtl_columns([]) == []


def test_sort_rtl_columns_keeps_close_x_in_same_bucket():
    """Regression: lines whose mean-x (cx) differs by a few pixels must
    stay in the same column cluster. The preprocessed CJK newspaper
    smoke test surfaced this — 今日新聞 at cx=1049 was being bucketed
    separately from 頭版頭條 at cx=1052 because
    ``int(round(cx // 10)) * 10`` truncates 1049 // 10 to 104.

    Boxes use tight, realistic coordinates (4 corners within a few
    pixels of the text) so cx ≈ the leading x of the text line.
    """
    if helper is None:
        pytest.skip("helper not available")
    a = {"box": [(1049, 80), (1078, 80), (1078, 120), (1049, 120)], "text": "A"}
    b = {"box": [(1052, 180), (1080, 180), (1080, 220), (1052, 220)], "text": "B"}
    c = {"box": [(1055, 280), (1085, 280), (1085, 320), (1055, 320)], "text": "C"}
    sorted_ = helper.sort_rtl_columns([b, a, c])
    assert [ln["text"] for ln in sorted_] == ["A", "B", "C"]


def test_sort_rtl_columns_splits_distant_columns():
    """Two clusters far apart in x form two columns. Reading order is
    rightmost first (RTL), top-to-bottom within each column.
    """
    if helper is None:
        pytest.skip("helper not available")
    # Right column (cx ≈ 1050)
    ra = {"box": [(1050, 80), (1100, 80), (1100, 120), (1050, 120)], "text": "RA"}
    rb = {"box": [(1052, 180), (1102, 180), (1102, 220), (1052, 220)], "text": "RB"}
    # Left column (cx ≈ 200)
    la = {"box": [(200, 100), (250, 100), (250, 140), (200, 140)], "text": "LA"}
    lb = {"box": [(202, 200), (252, 200), (252, 240), (202, 240)], "text": "LB"}
    sorted_ = helper.sort_rtl_columns([la, ra, lb, rb])
    # Right column first (RTL), then left column
    assert [ln["text"] for ln in sorted_] == ["RA", "RB", "LA", "LB"]


# ===========================================================================
# Parse result
# ===========================================================================


def test_parse_result_dict_format():
    if helper is None:
        pytest.skip("helper not available")
    raw = [
        [
            {"box": [(0, 0), (10, 0), (10, 20), (0, 20)], "text": "hello"},
            {"box": [(20, 0), (30, 0), (30, 20), (20, 20)], "text": "world"},
        ]
    ]
    lines = helper._parse_result(raw)
    assert len(lines) == 2
    assert lines[0]["text"] == "hello"
    assert lines[1]["text"] == "world"


def test_parse_result_list_format():
    if helper is None:
        pytest.skip("helper not available")
    raw = [
        [
            [[(0, 0), (10, 0), (10, 20), (0, 20)], ("hello", 0.95)],
        ]
    ]
    lines = helper._parse_result(raw)
    assert len(lines) == 1
    assert lines[0]["text"] == "hello"


def test_parse_result_empty_entry():
    if helper is None:
        pytest.skip("helper not available")
    raw = [[None]]
    assert helper._parse_result(raw) == []


# ===========================================================================
# GC — _scan_media
# ===========================================================================


def test_gc_scan_finds_old_files(tmp_path):
    if helper is None:
        pytest.skip("helper not available")
    d = tmp_path / "media"
    d.mkdir()
    now = time.time()
    (d / "ancient.png").write_text("x")
    os.utime(d / "ancient.png", (now, now - 100 * 86400))

    helper.GC_DIRS = [d]
    res = helper._scan_media(age_days=90, dry_run=True)
    assert res["count"] == 1
    assert "ancient.png" in res["candidates"][0]["path"]


def test_gc_remove_deletes_old_files(tmp_path):
    if helper is None:
        pytest.skip("helper not available")
    d = tmp_path / "media"
    d.mkdir()
    now = time.time()
    (d / "ancient.png").write_text("x")
    os.utime(d / "ancient.png", (now, now - 100 * 86400))

    helper.GC_DIRS = [d]
    res = helper._scan_media(age_days=90, dry_run=False)
    assert res["removed"] == 1
    assert res["freed_bytes"] > 0
    assert not (d / "ancient.png").exists()


def test_gc_noop_when_nothing_old(tmp_path):
    if helper is None:
        pytest.skip("helper not available")
    d = tmp_path / "media"
    d.mkdir()
    now = time.time()
    (d / "recent.png").write_text("x")
    os.utime(d / "recent.png", (now, now - 10 * 86400))

    helper.GC_DIRS = [d]
    res = helper._scan_media(age_days=90, dry_run=True)
    assert res["count"] == 0


def test_gc_skips_non_image_extensions(tmp_path):
    if helper is None:
        pytest.skip("helper not available")
    d = tmp_path / "media"
    d.mkdir()
    now = time.time()
    (d / "readme.txt").write_text("ignore")
    os.utime(d / "readme.txt", (now, now - 200 * 86400))

    helper.GC_DIRS = [d]
    res = helper._scan_media(age_days=30, dry_run=True)
    assert res["count"] == 0


# ===========================================================================
# CLI: verify command
# ===========================================================================


# ===========================================================================
# Image-level column-gap detection
# ===========================================================================


def _make_synthetic_image(width, height, col_starts, col_widths):
    """Return a synthetic RGB image with black column rectangles on white."""
    import numpy as np

    img = np.ones((height, width, 3), dtype=np.uint8) * 255
    for x_start, w in zip(col_starts, col_widths):
        img[:, x_start : x_start + w] = [0, 0, 0]
    return img


def _require_cv2():
    """Skip test if OpenCV is not available."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        pytest.skip("cv2 not available")


def test_detect_columns_two_column_finds_gap():
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    img = _make_synthetic_image(400, 200, col_starts=[10, 250], col_widths=[100, 100])
    gaps = helper.detect_columns_image(img)
    assert len(gaps) >= 1


def test_detect_columns_single_column_no_gap():
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    img = _make_synthetic_image(300, 150, col_starts=[10], col_widths=[280])
    gaps = helper.detect_columns_image(img)
    assert len(gaps) == 0


def test_detect_columns_low_ink_returns_empty():
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    import numpy as np

    img = np.ones((200, 300, 3), dtype=np.uint8) * 255
    img[50, 50] = [0, 0, 0]  # single pixel
    gaps = helper.detect_columns_image(img)
    assert len(gaps) == 0


def test_detect_columns_margin_ignored():
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    # Content centered with margins on both sides; no internal gap
    img = _make_synthetic_image(400, 200, col_starts=[60], col_widths=[280])
    gaps = helper.detect_columns_image(img)
    assert len(gaps) == 0


def test_detect_columns_three_column():
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    # Three columns = two gaps
    img = _make_synthetic_image(500, 200,
                                col_starts=[10, 180, 350],
                                col_widths=[80, 80, 80])
    gaps = helper.detect_columns_image(img)
    assert len(gaps) >= 2


def _make_text_blocks_image(width, height, block_h, col_starts, col_widths):
    """Return synthetic RGB image with character-height black blocks on white.

    Unlike ``_make_synthetic_image`` which uses full-height column
    rectangles, this produces blocks of height *block_h* so that
    ``_estimate_font_height`` can measure them.
    """
    import numpy as np

    img = np.ones((height, width, 3), dtype=np.uint8) * 255
    row_centers = list(range(block_h, height - block_h, block_h + block_h // 2))
    for x_start, w in zip(col_starts, col_widths):
        for cy in row_centers:
            r0 = max(0, cy - block_h // 2)
            r1 = min(height, cy + block_h // 2)
            img[r0:r1, x_start : x_start + w] = [0, 0, 0]
    return img


# ===========================================================================
# Font height estimation & adaptive sigma
# ===========================================================================


def test_estimate_font_height_normal():
    """Synthetic text blocks with known height → median matches."""
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    import numpy as np, cv2 as _cv2

    binary = np.zeros((200, 200), dtype=np.uint8)
    # Two blocks of height 20
    binary[30:50, 20:60] = 255
    binary[80:100, 20:60] = 255
    font_h = helper._estimate_font_height(binary)
    assert 15 <= font_h <= 25, f"expected ~20, got {font_h}"


def test_estimate_font_height_blank_returns_zero():
    """All-white image → no components → 0."""
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    import numpy as np, cv2 as _cv2

    binary = np.zeros((200, 200), dtype=np.uint8)
    assert helper._estimate_font_height(binary) == 0


def test_estimate_font_height_noise_ignored():
    """Tiny speckles are filtered out; only real-size block counts."""
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    import numpy as np, cv2 as _cv2

    binary = np.zeros((200, 200), dtype=np.uint8)
    # One real block: 20 px tall
    binary[50:70, 50:80] = 255
    # Speckle noise: 2 px tall
    binary[100:102, 100:102] = 255
    assert helper._estimate_font_height(binary) == 20


def test_detect_columns_sigma_scales_with_font():
    """font_h=40 → sigma ≈ 16."""
    if helper is None:
        pytest.skip("helper not available")
    assert helper._detect_columns_sigma(4000, 40) == 16.0


def test_detect_columns_sigma_fallback_on_zero_font():
    """font_h=0 → fallback to width-based formula."""
    if helper is None:
        pytest.skip("helper not available")
    # w=400 → min(400/60=6.67, 25) = 20/3 → max(3, 20/3) = 20/3
    import pytest as _pytest
    assert helper._detect_columns_sigma(400, 0) == _pytest.approx(20 / 3)


def test_detect_columns_high_dpi_equivalent():
    """Wide image with appropriately-sized text blocks finds columns."""
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    # Simulate 1200 DPI: wide image, blocks scaled proportionally
    img = _make_text_blocks_image(2000, 600, block_h=40,
                                  col_starts=[40, 700, 1400],
                                  col_widths=[300, 300, 300])
    gaps = helper.detect_columns_image(img)
    assert len(gaps) >= 2, f"expected ≥2 column gaps, got {gaps}"


def test_detect_columns_low_dpi_equivalent():
    """Narrow image with small text blocks still finds columns."""
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    # Simulate 72 DPI: narrow image, small blocks
    img = _make_text_blocks_image(400, 150, block_h=10,
                                  col_starts=[10, 140, 280],
                                  col_widths=[60, 60, 60])
    gaps = helper.detect_columns_image(img)
    assert len(gaps) >= 2, f"expected ≥2 column gaps, got {gaps}"


def test_process_raw_image_layer_overrides_wide_bboxes():
    if helper is None:
        pytest.skip("helper not available")
    # Wide bboxes that would normally be LTR
    raw = [[{"box": [(0, 0), (80, 0), (80, 20), (0, 20)], "text": "A"}] * 8]
    # But image shows a column gap -> should override to RTL
    paragraphs, layout = helper._process_raw(raw, img_columns=[(100, 120)])
    assert layout == "rtl-column-first"


def test_process_raw_image_layer_empty_is_fallback():
    if helper is None:
        pytest.skip("helper not available")
    # No image columns -> falls back to bbox heuristic
    raw = [[{"box": [(0, 0), (80, 0), (80, 20), (0, 20)], "text": "A"}] * 8]
    paragraphs, layout = helper._process_raw(raw, img_columns=[])
    assert layout == "ltr"


# ===========================================================================
# Multi-language — CJK gating
# ===========================================================================


def test_process_raw_non_cjk_disables_rtl():
    if helper is None:
        pytest.skip("helper not available")
    # Wide bboxes with img_columns would normally be RTL, but lang="en" -> LTR
    raw = [[{"box": [(0, 0), (80, 0), (80, 20), (0, 20)], "text": "A"}] * 8]
    paragraphs, layout = helper._process_raw(raw, img_columns=[(100, 120)], lang="en")
    assert layout == "ltr"


def test_process_raw_cjk_enables_rtl():
    if helper is None:
        pytest.skip("helper not available")
    # Same wide bboxes with img_columns, lang="ja" -> RTL
    raw = [[{"box": [(0, 0), (80, 0), (80, 20), (0, 20)], "text": "A"}] * 8]
    paragraphs, layout = helper._process_raw(raw, img_columns=[(100, 120)], lang="ja")
    assert layout == "rtl-column-first"


# ===========================================================================
# Parallel batch
# ===========================================================================


def test_resolve_jobs_explicit():
    if helper is None:
        pytest.skip("helper not available")
    assert helper._resolve_jobs(4) == 4
    assert helper._resolve_jobs(1) == 1
    assert helper._resolve_jobs(99) == 99


def test_resolve_jobs_auto_returns_one_or_more():
    if helper is None:
        pytest.skip("helper not available")
    assert helper._resolve_jobs(0) >= 1


# ===========================================================================
# Auto preprocess decision
# ===========================================================================


def _make_gray_image(width, height, pixel_value):
    """Return a grayscale synthetic image with uniform pixel_value (0–255)."""
    import numpy as np

    return np.full((height, width), pixel_value, dtype=np.uint8)


def test_auto_preprocess_clean_high_contrast_skips_thresholding():
    """Nearly-binary image (e.g. synthetic 28 px CJK text) → binarize=False."""
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    # Mostly white with some black pixels — near-extremal ratio is high.
    img = _make_gray_image(200, 100, 240)
    img[10:15, 20:25] = 0  # small dark patch
    d, b, n, cv, er = helper._auto_preprocess_decision(img)
    assert b is False, "clean high-contrast image should skip binarization"


def test_auto_preprocess_noisy_photo_enables_thresholding():
    """Low contrast / high local CV → binarize=True."""
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    import numpy as np

    # Mid-gray with moderate noise — no near-extremal pixels, high CV
    rng = np.random.default_rng(42)
    img = rng.integers(60, 120, size=(200, 200), dtype=np.uint8)
    d, b, n, cv, er = helper._auto_preprocess_decision(img)
    assert b is True, "noisy mid-gray image should enable binarization"


def test_auto_preprocess_decision_always_deskews():
    """Deskew flag should always be True."""
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    import numpy as np

    img = np.full((100, 100), 128, dtype=np.uint8)
    d, b, n, cv, er = helper._auto_preprocess_decision(img)
    assert d is True


def test_auto_preprocess_denoise_tracks_binarize():
    """Denoise flag should always match binarize flag."""
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    import numpy as np

    # Uniform gray → low extremal ratio → both True
    img = np.full((100, 100), 128, dtype=np.uint8)
    d1, b1, n1, cv1, er1 = helper._auto_preprocess_decision(img)
    assert n1 is b1

    # Nearly binary → both False
    img2 = _make_gray_image(200, 100, 240)
    img2[10:15, 20:25] = 0
    d2, b2, n2, cv2, er2 = helper._auto_preprocess_decision(img2)
    assert n2 is b2


def test_auto_preprocess_decision_returns_heuristic_values():
    """Returns (d, b, n, contrast_cv, extremal_ratio) with floats in range."""
    _require_cv2()
    if helper is None:
        pytest.skip("helper not available")
    import numpy as np
    rng = np.random.default_rng(42)
    img = rng.integers(60, 120, size=(200, 200), dtype=np.uint8)
    d, b, n, cv, er = helper._auto_preprocess_decision(img)
    assert isinstance(cv, float)
    assert isinstance(er, float)
    assert 0.0 <= cv <= 10.0
    assert 0.0 <= er <= 1.0


# ===========================================================================
# Preprocess metadata
# ===========================================================================


def _make_meta(**kw):
    """Build a minimal _preprocess_meta dict for test results."""
    return {"mode": kw.get("mode", "auto"), "deskew": kw.get("deskew", False),
            "binarize": kw.get("binarize", False), "denoise": kw.get("denoise", False)}


def test_json_output_includes_preprocess_block():
    """json-format _print_result surfaces _preprocess_meta as 'preprocess'."""
    if helper is None:
        pytest.skip("helper not available")
    import io, sys
    result = {
        "filename": "test.jpg",
        "layout": "ltr",
        "paragraphs": ["hello"],
        "chars": 5,
        "_preprocess_meta": _make_meta(mode="off"),
    }
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        helper._print_result(result, "json")
    finally:
        sys.stdout = old
    import json as _json
    parsed = _json.loads(buf.getvalue().strip())
    assert "preprocess" in parsed
    assert parsed["preprocess"]["mode"] == "off"


def test_json_output_strips_lines():
    """json output does not include _lines key."""
    if helper is None:
        pytest.skip("helper not available")
    import io, sys, json as _json
    result = {
        "filename": "test.jpg",
        "layout": "ltr",
        "paragraphs": ["hi"],
        "chars": 2,
        "_lines": [{"text": "hi", "confidence": 0.9}],
    }
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        helper._print_result(result, "json")
    finally:
        sys.stdout = old
    parsed = _json.loads(buf.getvalue().strip())
    assert "_lines" not in parsed
    assert parsed["filename"] == "test.jpg"


def test_text_output_ignores_preprocess_meta():
    """Text-format output does not crash when _preprocess_meta is present."""
    if helper is None:
        pytest.skip("helper not available")
    import io, sys
    result = {
        "filename": "test.jpg",
        "layout": "ltr",
        "paragraphs": ["hello"],
        "chars": 5,
        "_preprocess_meta": _make_meta(mode="off"),
    }
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        helper._print_result(result, "text")
    finally:
        sys.stdout = old
    output = buf.getvalue()
    assert "hello" in output
    assert "preprocess" not in output


# ===========================================================================
# Confidence extraction in _parse_result
# ===========================================================================


def test_parse_result_dict_format_includes_confidence():
    """New-format dict entries get per-line confidence extracted."""
    if helper is None:
        pytest.skip("helper not available")
    raw = [
        [
            {"box": [(0, 0), (10, 0), (10, 20), (0, 20)], "text": "hello",
             "confidence": 0.95},
            {"box": [(20, 0), (30, 0), (30, 20), (20, 20)], "text": "world",
             "confidence": 0.87},
        ]
    ]
    lines = helper._parse_result(raw)
    assert len(lines) == 2
    assert lines[0]["confidence"] == 0.95
    assert lines[1]["confidence"] == 0.87


def test_parse_result_dict_missing_confidence_defaults_zero():
    """Dict entries without confidence key get 0.0 (not a crash)."""
    if helper is None:
        pytest.skip("helper not available")
    raw = [[
        {"box": [(0, 0), (10, 0), (10, 20), (0, 20)], "text": "no_conf"},
    ]]
    lines = helper._parse_result(raw)
    assert lines[0]["confidence"] == 0.0


def test_parse_result_legacy_format_confidence():
    """Legacy [[box, (text, score)]] format extracts the score."""
    if helper is None:
        pytest.skip("helper not available")
    raw = [[
        [[(0, 0), (10, 0), (10, 20), (0, 20)], ("hello", 0.95)],
    ]]
    lines = helper._parse_result(raw)
    assert lines[0]["confidence"] == 0.95


def test_parse_result_new_format_confidence():
    """New v3.7+ format with rec_scores array."""
    if helper is None:
        pytest.skip("helper not available")
    raw = [
        {
            "rec_texts": ["hello", "world"],
            "rec_polys": [[[0, 0], [10, 0], [10, 20], [0, 20]],
                          [[20, 0], [30, 0], [30, 20], [20, 20]]],
            "rec_scores": [0.98, 0.76],
        }
    ]
    lines = helper._parse_result(raw)
    assert len(lines) == 2
    assert lines[0]["confidence"] == 0.98
    assert lines[1]["confidence"] == 0.76


# ===========================================================================
# Quality score
# ===========================================================================


def test_quality_score_empty_result():
    if helper is None:
        pytest.skip("helper not available")
    assert helper._quality_score({}) == -1.0
    assert helper._quality_score({"paragraphs": []}) == -1.0


def test_quality_score_no_lines_fallback():
    """Without _lines, fallback score is proportional to chars (capped 0.5)."""
    if helper is None:
        pytest.skip("helper not available")
    r = {"paragraphs": ["hello world"], "chars": 11}
    score = helper._quality_score(r)
    assert 0.0 < score <= 0.5


def test_quality_score_high_confidence():
    """Happy path: median confidence of lines."""
    if helper is None:
        pytest.skip("helper not available")
    r = {
        "paragraphs": ["hello world"],
        "chars": 11,
        "_lines": [
            {"confidence": 0.95},
            {"confidence": 0.87},
            {"confidence": 0.92},
        ],
    }
    # sorted: 0.87, 0.92, 0.95 → median = 0.92
    score = helper._quality_score(r)
    assert score == 0.92


def test_quality_score_low_chars_low_confidence_returns_negative():
    """Fewer than 5 chars with median < 0.6 → -1.0 (garbage guard)."""
    if helper is None:
        pytest.skip("helper not available")
    r = {
        "paragraphs": ["ab"],  # 2 chars
        "chars": 2,
        "_lines": [
            {"confidence": 0.45},
            {"confidence": 0.32},
        ],
    }
    assert helper._quality_score(r) == -1.0


def test_quality_score_low_chars_but_high_confidence_not_negative():
    """Few chars but high confidence is OK (valid short text like 'OK')."""
    if helper is None:
        pytest.skip("helper not available")
    r = {
        "paragraphs": ["OK"],
        "chars": 2,
        "_lines": [
            {"confidence": 0.95},
            {"confidence": 0.92},
        ],
    }
    assert helper._quality_score(r) >= 0


# ===========================================================================
# Opposite preprocess resolution
# ===========================================================================


def test_resolve_opposite_on_off():
    if helper is None:
        pytest.skip("helper not available")
    assert helper._resolve_opposite_preprocess("on") == "off"
    assert helper._resolve_opposite_preprocess("off") == "on"


def test_resolve_opposite_auto_uses_decided():
    """auto with auto_decided='on' resolves to 'off'."""
    if helper is None:
        pytest.skip("helper not available")
    assert helper._resolve_opposite_preprocess("auto", "on") == "off"
    assert helper._resolve_opposite_preprocess("auto", "off") == "on"


def test_resolve_opposite_auto_requires_decided():
    """auto without auto_decided raises ValueError."""
    if helper is None:
        pytest.skip("helper not available")
    import pytest as _pytest
    with _pytest.raises(ValueError):
        helper._resolve_opposite_preprocess("auto")


# ===========================================================================
# CLI: verify command
# ===========================================================================


def test_cmd_smartocr_verify_no_paddle(monkeypatch):
    """Verify check fails gracefully when PaddleOCR is missing.

    Putting ``None`` in ``sys.modules['paddleocr']`` makes Python
    raise ``ImportError`` on any subsequent ``import paddleocr`` —
    this is the canonical way to make the test deterministic
    regardless of whether PaddleOCR is actually installed.
    """
    import importlib
    import sys

    from hermes_cli import smartocr as smartocr_mod

    monkeypatch.setitem(sys.modules, "paddleocr", None)

    reloaded = importlib.reload(smartocr_mod)
    try:
        with pytest.raises(SystemExit) as exc:
            reloaded._cmd_verify()
        assert exc.value.code == 1
    finally:
        monkeypatch.delitem(sys.modules, "paddleocr", raising=False)
        importlib.reload(smartocr_mod)


def test_cmd_smartocr_verify_with_paddle(monkeypatch, capsys):
    """Verify check succeeds (no SystemExit) when PaddleOCR is importable.

    Mirrors the negative test — stubs paddleocr as a fake module that
    imports cleanly so we exercise the happy path deterministically.
    """
    import importlib
    import sys
    import types

    from hermes_cli import smartocr as smartocr_mod

    fake = types.ModuleType("paddleocr")
    monkeypatch.setitem(sys.modules, "paddleocr", fake)

    reloaded = importlib.reload(smartocr_mod)
    try:
        reloaded._cmd_verify()
    finally:
        monkeypatch.delitem(sys.modules, "paddleocr", raising=False)
        importlib.reload(smartocr_mod)

    out = capsys.readouterr().out
    assert "PaddleOCR is installed" in out
