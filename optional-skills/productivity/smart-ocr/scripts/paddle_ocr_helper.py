#!/usr/bin/env python3
"""Smart OCR helper — PaddleOCR + RTL/CJK column fix + media garbage-collection.

Fixes the classic CJK newspaper bug: right-to-left vertical columns get
merged line-by-line into garbage.  This helper detects vertical layout,
sorts lines R→L then T→B, and outputs proper paragraphs.

Usage::

    python paddle_ocr_helper.py ocr newspaper.png          # image
    python paddle_ocr_helper.py ocr document.pdf           # PDF (multi-page)
    python paddle_ocr_helper.py gc --scan                  # dry-run old media
    python paddle_ocr_helper.py gc --remove                # actually delete old media

Install once::

    pip install paddlepaddle Pillow opencv-python-headless
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
#  PaddleOCR — lazy import
# ---------------------------------------------------------------------------

_PADDLE_OCR_CACHE: dict[str, "PaddleOCR"] = {}


def _load_paddle(lang: str = "ch"):
    if lang in _PADDLE_OCR_CACHE:
        return _PADDLE_OCR_CACHE[lang]
    try:
        from paddleocr import PaddleOCR

        ocr = PaddleOCR(lang=lang)
        _PADDLE_OCR_CACHE[lang] = ocr
        return ocr
    except ImportError as e:
        raise SystemExit(
            "PaddleOCR not installed.\n"
            "  pip install paddlepaddle Pillow opencv-python-headless"
        ) from e


_CJK_LANGS = frozenset({"ch", "chinese_cht", "japan", "ja", "korean", "ko"})


# ---------------------------------------------------------------------------
#  OCR line parsing helpers
# ---------------------------------------------------------------------------


def _parse_result(raw):
    """Turn PaddleOCR output into list of dicts with 'box', 'text', and 'confidence'.

    Handles two formats:
    - v3.7+:  [{"rec_texts": [...], "rec_polys": [...], "rec_scores": [...]}, ...]
    - legacy: [[[box, (text, score)], ...]]

    Returns list of dicts, each with ``"box"``, ``"text"``, ``"confidence"``.
    """
    lines = []
    if not isinstance(raw, (list, tuple)):
        return lines
    if not raw:
        return lines

    first = raw[0]

    if isinstance(first, dict) or (hasattr(first, "get") and "rec_texts" in first):
        texts = first.get("rec_texts") or []
        polys = first.get("rec_polys") or []
        scores = first.get("rec_scores") or []
        for i in range(len(texts)):
            txt = texts[i]
            box = polys[i].tolist() if hasattr(polys[i], "tolist") else polys[i]
            if not txt or not box:
                continue
            conf = float(scores[i]) if i < len(scores) and scores[i] is not None else 0.0
            lines.append({"box": box, "text": txt.strip(), "confidence": conf})
        return lines

    if not first:
        return lines

    for entry in first:
        if isinstance(entry, dict):
            box = entry.get("box") or []
            txt = entry.get("text") or ""
            conf = float(entry.get("confidence", 0) or 0)
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            box = entry[0] if isinstance(entry[0], (list, tuple)) else []
            raw_txt = entry[1]
            if isinstance(raw_txt, str):
                txt = raw_txt
                conf = 0.0
            elif isinstance(raw_txt, (list, tuple)) and len(raw_txt) >= 1:
                txt = str(raw_txt[0])
                conf = float(raw_txt[1]) if len(raw_txt) >= 2 and raw_txt[1] is not None else 0.0
            else:
                txt = ""
                conf = 0.0
        else:
            continue

        if not box or not txt:
            continue

        lines.append({"box": box, "text": txt.strip(), "confidence": conf})

    return lines


def _tall_narrow_ratio(lines):
    """Proportion of lines whose bounding box is narrow & tall (vertical CJK)."""
    if not lines:
        return 0.0
    count = 0
    for ln in lines:
        bbox = ln.get("box")
        if not isinstance(bbox, list) or len(bbox) < 2:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        w = max(xs) - min(xs) + 1e-6
        h = max(ys) - min(ys) + 1e-6
        if h > w * 2:
            count += 1
    return count / len(lines)


def detect_rtl_layout(lines):
    """Return True if >40% of lines are tall/narrow -> RTL column layout."""
    return _tall_narrow_ratio(lines) > 0.4


def sort_rtl_columns(lines):
    """Group by x-position, columns right->left, within each column top->bottom."""
    buckets = defaultdict(list)
    for ln in lines:
        bbox = ln.get("box")
        if not isinstance(bbox, list) or len(bbox) < 2:
            continue
        cx = sum(p[0] for p in bbox) / len(bbox)
        key = int(round(cx // 10)) * 10
        buckets[key].append(ln)

    ordered = []
    for key in sorted(buckets, reverse=True):
        col = sorted(buckets[key], key=lambda x: sum(p[1] for p in x["box"]) / len(x["box"]))
        ordered.extend(col)
    return ordered


def _process_raw(raw, *, img_columns=None, lang="ch"):
    """Parse OCR result, detect layout, sort columns, return (paragraphs, layout).

    *img_columns*: optional list of (gap_start, gap_end) from
    detect_columns_image().  When gaps are present the image is
    multi-column and RTL ordering is applied for CJK content (*lang*).

    *lang*: language code passed to PaddleOCR.  RTL layout detection
    (both image-level and bbox-based) only fires for CJK languages.
    """
    lines = _parse_result(raw)
    if not lines:
        return [], "empty"

    rtl = False

    # RTL only applies to CJK content
    if lang in _CJK_LANGS:
        # Layer 1: image-level column-gap detection (most reliable signal)
        if img_columns and len(img_columns) >= 1:
            rtl = True  # multi-column CJK -> RTL

        # Layer 2: fallback to OCR bbox tall/narrow heuristic
        if not rtl:
            rtl = detect_rtl_layout(lines)

    if rtl:
        lines = sort_rtl_columns(lines)

    return [ln["text"] for ln in lines], "rtl-column-first" if rtl else "ltr"


# ---------------------------------------------------------------------------
#  Image preprocessing pipeline
# ---------------------------------------------------------------------------


def deskew(img):
    """Detect and correct skew angle in a grayscale/RGB image.

    Returns (corrected_image, angle_degrees).
    """
    import cv2
    import numpy as np

    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLines(edges, 1, np.pi / 180, 300)

    if lines is None:
        return img, 0.0

    angles = []
    for line in lines:
        rho, theta = line[0]
        angle = np.rad2deg(theta) - 90
        if abs(angle) < 45:
            angles.append(angle)

    if not angles:
        return img, 0.0

    median_angle = np.median(angles)
    if abs(median_angle) < 0.5:
        return img, 0.0

    h, w = img.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, median_angle, 1.0)
    # Compute new bounds so no content is cropped
    cos = abs(M[0, 0])
    sin = abs(M[0, 1])
    nw = int(h * sin + w * cos)
    nh = int(h * cos + w * sin)
    M[0, 2] += nw / 2 - center[0]
    M[1, 2] += nh / 2 - center[1]
    rotated = cv2.warpAffine(img, M, (nw, nh), borderMode=cv2.BORDER_REPLICATE)
    return rotated, round(median_angle, 2)


def binarize_adaptive(img, block_size=31, c=10):
    """Adaptive Gaussian thresholding; *block_size* must be odd."""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    if block_size % 2 == 0:
        block_size += 1
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, c
    )
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2RGB)


def denoise(img, strength="light"):
    """Reduce noise.  light=medianBlur(3), heavy=fastNlMeansDenoising."""
    import cv2
    import numpy as np

    if strength == "off":
        return img
    if strength == "light":
        return cv2.medianBlur(img, 3)
    return cv2.fastNlMeansDenoisingColored(img, None, 10, 10, 7, 21)


def preprocess_image(img, deskew_flag=True, binarize_flag=True, denoise_flag=True, denoise_strength="light"):
    """Apply deskew -> adaptive binarization -> denoise in sequence.

    *img*: RGB numpy array (H, W, 3).  Returns (processed RGB array, meta dict).
    """
    import cv2
    import numpy as np

    if not isinstance(img, np.ndarray):
        raise TypeError("Expected numpy array, got %s" % type(img).__name__)

    meta: dict[str, bool | float] = {
        "deskew": False,
        "binarize": False,
        "denoise": False,
    }

    if deskew_flag:
        img, angle = deskew(img)
        meta["deskew"] = True
        meta["deskew_angle"] = round(angle, 2)

    if binarize_flag:
        img = binarize_adaptive(img)
        meta["binarize"] = True

    if denoise_flag and denoise_strength != "off":
        img = denoise(img, strength=denoise_strength)
        meta["denoise"] = True
        if denoise_strength != "light":
            meta["denoise_strength"] = denoise_strength

    return img, meta


# ---------------------------------------------------------------------------
#  Auto preprocess decision (image quality heuristics)
# ---------------------------------------------------------------------------


def _auto_preprocess_decision(img_rgb):
    """Analyse *img_rgb* and return (deskew, binarize, denoise) flags.

    The goal is to skip binarization for clean, high-contrast images
    where it degrades thin strokes (e.g. synthetic 28 px CJK text),
    and apply it for noisy, unevenly-lit scanned documents.

    Heuristics (all computed on the grayscale version):
    1. Near-extremal pixel ratio — pixels <30 or >225 as fraction of total.
       High ratio → already near-binary → skip binarization.
    2. Local contrast coefficient of variation — std of 16×16-block stds
       divided by mean block std.  High CV → uneven lighting → binarize.
    """
    import cv2
    import numpy as np

    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY) if img_rgb.ndim == 3 else img_rgb
    h, w = gray.shape

    # --- Near-extremal fraction ---
    dark = int((gray < 30).sum())
    bright = int((gray > 225).sum())
    extremal_ratio = (dark + bright) / (h * w)

    # --- Local contrast CV (16x16 blocks) ---
    bx = max(1, w // 16)
    by = max(1, h // 16)
    block_stds = []
    for y in range(by):
        row_start = y * 16
        row_end = min(row_start + 16, h)
        for x in range(bx):
            col_start = x * 16
            col_end = min(col_start + 16, w)
            blk = gray[row_start:row_end, col_start:col_end]
            block_stds.append(blk.std())
    arr = np.array(block_stds, dtype=np.float64)
    contrast_cv = arr.std() / (arr.mean() + 1e-6)

    binarize = extremal_ratio < 0.95 or contrast_cv > 0.4
    # Returns (deskew, binarize, denoise, contrast_cv, extremal_ratio)
    # deskew always on; denoise matches binarize
    return True, binarize, binarize, round(contrast_cv, 4), round(extremal_ratio, 4)


# ---------------------------------------------------------------------------
#  Quality scoring & retry
# ---------------------------------------------------------------------------


def _quality_score(result: dict) -> float:
    """Return a quality score for an OCR result dict.

    *  ``-1.0`` — total failure (no chars or suspiciously few with low
       confidence).  The caller should retry with a different strategy.
    *  ``0.0`` – ``1.0`` — median confidence of all recognised characters.
       Higher is better.

    The median is robust against a handful of garbage tokens (e.g. noise
    patches the model mistook for text with low confidence).
    """
    paragraphs = result.get("paragraphs") or []
    if not paragraphs:
        return -1.0
    chars = sum(len(p) for p in paragraphs)
    if chars == 0:
        return -1.0

    # Collect per-character confidences (reconstructed from line data
    # stored in a ``_lines`` key if available, otherwise estimate from
    # paragraph count alone).
    lines = result.get("_lines")
    if not lines:
        # Fallback: small positive score proportional to char count so
        # a paragraph with content beats an empty one, but capped at
        # 0.5 so a retry with full confidence can still win.
        return min(0.5, chars / 1000.0)

    confs = [ln.get("confidence", 0.0) for ln in lines if ln.get("confidence") is not None]
    if not confs:
        return -1.0

    # Guard: very few chars with uniformly low confidence → garbage
    confs_sorted = sorted(confs)
    n = len(confs_sorted)
    median = confs_sorted[n // 2] if n % 2 == 1 else (confs_sorted[n // 2 - 1] + confs_sorted[n // 2]) / 2.0

    if chars < 5 and median < 0.6:
        return -1.0

    return median


def _resolve_opposite_preprocess(mode: str, auto_decided: str | None = None) -> str:
    """Return the opposite preprocessing mode to *mode*.

    When *mode* is ``"auto"``, *auto_decided* is the per-image decision
    (``"on"`` or ``"off"``) so the retry flips the concrete decision
    rather than looping back through auto.

    *auto_decided* must be provided when *mode* is ``"auto"``.
    """
    if mode == "auto":
        if auto_decided is None:
            raise ValueError("auto_decided is required when mode='auto'")
        return "off" if auto_decided == "on" else "on"
    return "off" if mode == "on" else "on"


def _ocr_with_retry(ocr_fn, retry: bool, primary_mode: str, auto_decided: str | None,
                    *args, **kw) -> dict:
    """Run *ocr_fn*, retrying with the opposite preprocessing when quality is low.

    *ocr_fn* is a callable that accepts (preprocess, ...) and returns a
    result dict with at least ``"paragraphs"``, ``"chars"``, and
    ``"_lines"`` (the parsed lines from ``_parse_result``).

    ``*args, **kw`` are forwarded to *ocr_fn* minus ``preprocess=``.
    """
    result = ocr_fn(preprocess=primary_mode, *args, **kw)
    if not retry or "error" in result:
        return result

    score = _quality_score(result)
    if score >= 0:
        return result

    opposite = _resolve_opposite_preprocess(primary_mode, auto_decided)
    retry_result = ocr_fn(preprocess=opposite, *args, **kw)
    if "error" in retry_result:
        return result

    retry_score = _quality_score(retry_result)
    return retry_result if retry_score > score else result


# ---------------------------------------------------------------------------
#  Image-level column-gap detection (pre-OCR layout signal)
# ---------------------------------------------------------------------------


def _estimate_font_height(binary: np.ndarray) -> int:
    """Estimate median font height in pixels via connected components.

    Returns 0 when no suitable component is found (blank page, photo,
    low-ink — caller falls back to width-based heuristics).

    Filters out:
    *  Components with height < 5 px (speckle noise).
    *  Components taller than 50 % of image height (full-page graphics).
    *  Components with area < 10 px² (single-pixel noise).
    *  Components occupying >30 % of total image area (photos / large
       illustrations).
    """
    import cv2
    import numpy as np

    n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        binary, connectivity=8
    )
    if n_labels <= 1:
        return 0

    h_img, w_img = binary.shape
    total_area = h_img * w_img
    heights = []
    for i in range(1, n_labels):
        comp_h = stats[i, cv2.CC_STAT_HEIGHT]
        comp_area = stats[i, cv2.CC_STAT_AREA]
        if comp_h < 5:
            continue
        if comp_h > h_img * 0.5:
            continue
        if comp_area < 10:
            continue
        if comp_area > total_area * 0.3:
            continue
        heights.append(comp_h)

    if not heights:
        return 0
    return int(np.median(heights))


def _detect_columns_sigma(w: int, font_h: int) -> float:
    """Return a suitable Gaussian sigma for vertical-projection smoothing.

    When *font_h* > 0, sigma = max(0.4 * font_h, 1.0) — roughly 40 % of
    the median character height.  This is narrow enough to preserve
    column gaps (1.5–3× font_h) but wide enough to merge within-character
    stroke gaps (0.1–0.15× font_h) and inter-character gaps (0.2–0.3×).

    Fallback (font_h == 0): width-based formula ``max(3.0, min(w/60, 25))``.
    """
    if font_h > 0:
        return max(0.4 * font_h, 1.0)
    return max(3.0, min(w / 60.0, 25.0))


def detect_columns_image(img_rgb: np.ndarray) -> list[tuple[int, int]]:
    """Detect vertical column gaps via vertical projection profiles.

    Operates on the preprocessed image (deskewed + binarized preferred).
    Returns list of (gap_start, gap_end) pixel x-coordinates for each
    confirmed column gap, sorted left-to-right.  Empty list -> single-column.

    Parameters (sigma, margin, min_gap) scale with estimated font height
    when text content is detected, falling back to image-width heuristics.

    Algorithm:
    1. Convert to grayscale, OTSU binary (inverted so text = white).
    2. Estimate median font height via connected components.
    3. Vertical projection: sum of text pixels per column.
    4. Gaussian smooth (sigma derived from font height or image width).
    5. Search gaps only in effective page area (font-aware margin).
    6. Robust valley detection using P30 local percentile threshold.
    7. Merge nearby valleys; width gate confirms column gaps.
    """
    import cv2
    import numpy as np

    _DEBUG = os.environ.get("SMART_OCR_DEBUG") == "1"

    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY) if img_rgb.ndim == 3 else img_rgb
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    h, w = binary.shape

    # --- Low-ink guard ---
    ink_ratio = cv2.countNonZero(binary) / (h * w)
    if _DEBUG:
        print(f"[detect_columns] w={w}, h={h}, ink_ratio={ink_ratio:.4f}", file=sys.stderr)
    if ink_ratio < 0.005:
        return []

    # --- Estimate font height for adaptive parameters ---
    font_h = _estimate_font_height(binary)
    if _DEBUG:
        print(f"[detect_columns] estimated_font_height={font_h}", file=sys.stderr)

    # --- Vertical projection ---
    proj = binary.astype(np.float32).sum(axis=0) / 255.0  # shape: (W,)

    # --- Gaussian smooth (font-aware sigma) ---
    sigma = _detect_columns_sigma(w, font_h)
    ksize = int(sigma * 6) | 1
    kernel = cv2.getGaussianKernel(ksize, sigma)
    smoothed = cv2.filter2D(proj, -1, kernel.ravel())

    # --- Effective page area (font-aware margin) ---
    if font_h > 0:
        margin = max(int(w * 0.03), font_h * 3)
    else:
        margin = max(1, int(w * 0.05))
    inner = smoothed[margin : w - margin]
    if inner.size == 0:
        return []

    # --- Robust valley threshold: P30 of inner region ---
    sorted_vals = np.sort(inner)
    p30 = sorted_vals[int(len(sorted_vals) * 0.3)]
    threshold = max(p30, 1.0)

    # --- Find valleys ---
    in_gap = False
    valleys: list[tuple[int, int]] = []
    start = 0
    for x in range(margin, w - margin):
        if smoothed[x] < threshold:
            if not in_gap:
                in_gap = True
                start = x
        else:
            if in_gap:
                valleys.append((start, x))
                in_gap = False
    if in_gap:
        valleys.append((start, w - margin))

    if _DEBUG:
        print(f"[detect_columns] sigma={sigma:.1f}, ksize={ksize}, font_h={font_h},"
              f" margin={margin}, threshold={threshold:.1f}", file=sys.stderr)
        print(f"[detect_columns] valleys before merge: {valleys}", file=sys.stderr)

    # --- Merge nearby valleys ---
    if not valleys:
        return []
    if font_h > 0:
        min_gap = max(w // 40, font_h * 2, 12)
    else:
        min_gap = max(w // 30, 12)
    merged: list[tuple[int, int]] = []
    cur_start, cur_end = valleys[0]
    for s, e in valleys[1:]:
        if s - cur_end < min_gap // 2:
            cur_end = e
        else:
            if cur_end - cur_start >= min_gap:
                merged.append((cur_start, cur_end))
            cur_start, cur_end = s, e
    if cur_end - cur_start >= min_gap:
        merged.append((cur_start, cur_end))

    if _DEBUG:
        print(f"[detect_columns] merged gaps: {merged}", file=sys.stderr)

    return merged


# ---------------------------------------------------------------------------
#  OCR entry points
# ---------------------------------------------------------------------------


def ocr_image(image_path: str | Path, preprocess: str = "auto", denoise_strength: str = "light", lang: str = "ch", retry: bool = False) -> dict:
    """Run OCR on a single image file with optional preprocessing.

    *preprocess*: ``"auto"`` (default, decide per image), ``"on"`` (force
    all steps), or ``"off"`` (skip all).

    *lang*: PaddleOCR language code (default "ch").  RTL column sorting
    is only applied for CJK languages.

    *retry*: when ``True``, re-run with the opposite preprocessing strategy
    if the quality score is negative.

    Returns {filename, layout, paragraphs, chars, _lines, error}.
    """
    import cv2
    import numpy as np

    p = Path(image_path)
    if not p.exists():
        return {"error": f"File not found: {p}", "filename": str(p)}

    img = cv2.imread(str(p))
    if img is None:
        return {"error": f"Cannot read image: {p}", "filename": str(p)}

    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

    # Determine what "auto" actually decided before we mutate img_rgb
    auto_decided: str | None = None
    if preprocess == "auto":
        d, b, n, cv0, er0 = _auto_preprocess_decision(img_rgb)
        auto_decided = "on" if b else "off"

    def _run_once(pp_mode: str) -> dict:
        """Inner OCR call — accepts concrete preprocessing mode."""
        ppm: dict = {"mode": pp_mode}
        working = img_rgb.copy()
        if pp_mode == "on":
            working, ppm_steps = preprocess_image(working, denoise_strength=denoise_strength)
            ppm.update(ppm_steps)
        elif pp_mode == "off":
            ppm.update({"deskew": False, "binarize": False, "denoise": False})
        elif pp_mode == "auto":
            d2, b2, n2, cv2, er2 = _auto_preprocess_decision(working)
            ppm.update({"contrast_cv": cv2, "extremal_ratio": er2})
            working, ppm_steps = preprocess_image(working, deskew_flag=d2, binarize_flag=b2, denoise_flag=n2, denoise_strength=denoise_strength)
            ppm.update(ppm_steps)

        img_columns = detect_columns_image(working) if lang in _CJK_LANGS else None

        ocr = _load_paddle(lang)
        try:
            raw = ocr.predict(working)
        except Exception as e:
            return {"error": f"OCR failed: {e}", "filename": str(p)}

        lines = _parse_result(raw)
        paragraphs_text = [ln["text"] for ln in lines]

        paragraphs, layout = _process_raw(raw, img_columns=img_columns, lang=lang)
        return {
            "filename": str(p),
            "layout": layout,
            "paragraphs": paragraphs,
            "chars": sum(len(t) for t in paragraphs),
            "_lines": lines,
            "_preprocess_meta": ppm,
        }

    result = _run_once(preprocess)

    if retry and "error" not in result:
        score = _quality_score(result)
        if score < 0:
            opposite = _resolve_opposite_preprocess(preprocess, auto_decided)
            retry_result = _run_once(opposite)
            if "error" not in retry_result:
                retry_score = _quality_score(retry_result)
                if retry_score > score:
                    result = retry_result

    result.pop("_lines", None)
    return result


def ocr_pdf(pdf_path: str | Path, scale: float = 2.0, preprocess: str = "auto", denoise_strength: str = "light", lang: str = "ch", retry: bool = False) -> list[dict]:
    """Render each PDF page to image, optionally preprocess, OCR it.

    *preprocess*: ``"auto"`` (default, decide per image), ``"on"`` (force
    all steps), or ``"off"`` (skip all).

    *lang*: PaddleOCR language code (default "ch").  RTL column sorting
    is only applied for CJK languages.

    *retry*: when ``True``, re-run each page with the opposite preprocessing
    strategy if the quality score is negative.

    Returns list of dicts, each with page_number, layout, paragraphs, chars.
    """
    import numpy as np
    import pypdfium2 as pdfium

    p = Path(pdf_path)
    if not p.exists():
        return [{"error": f"File not found: {p}", "filename": str(p)}]

    ocr = _load_paddle(lang)
    doc = pdfium.PdfDocument(str(p))
    num_pages = len(doc)

    results = []
    for page_idx in range(num_pages):
        page = doc.get_page(page_idx)
        bitmap = page.render(scale=scale)
        pil_img = bitmap.to_pil()
        np_array = np.array(pil_img.convert("RGB"))

        # Determine what "auto" actually decided for this page
        auto_decided: str | None = None
        if preprocess == "auto":
            d, b, n, cv0, er0 = _auto_preprocess_decision(np_array)
            auto_decided = "on" if b else "off"

        def _run_once(pp_mode: str) -> dict:
            ppm: dict = {"mode": pp_mode}
            working = np_array.copy()
            if pp_mode == "on":
                working, ppm_steps = preprocess_image(working, denoise_strength=denoise_strength)
                ppm.update(ppm_steps)
            elif pp_mode == "off":
                ppm.update({"deskew": False, "binarize": False, "denoise": False})
            elif pp_mode == "auto":
                d2, b2, n2, cv2, er2 = _auto_preprocess_decision(working)
                ppm.update({"contrast_cv": cv2, "extremal_ratio": er2})
                working, ppm_steps = preprocess_image(working, deskew_flag=d2, binarize_flag=b2, denoise_flag=n2, denoise_strength=denoise_strength)
                ppm.update(ppm_steps)

            img_columns = detect_columns_image(working) if lang in _CJK_LANGS else None
            try:
                raw2 = ocr.predict(working)
            except Exception as e:
                return {"error": str(e), "filename": str(p), "page_number": page_idx + 1}

            lines = _parse_result(raw2)
            paragraphs, layout = _process_raw(raw2, img_columns=img_columns, lang=lang)
            return {
                "filename": str(p),
                "page_number": page_idx + 1,
                "layout": layout,
                "paragraphs": paragraphs,
                "chars": sum(len(t) for t in paragraphs),
                "_lines": lines,
                "_preprocess_meta": ppm,
            }

        result = _run_once(preprocess)

        if retry and "error" not in result:
            score = _quality_score(result)
            if score < 0:
                opposite = _resolve_opposite_preprocess(preprocess, auto_decided)
                retry_result = _run_once(opposite)
                if "error" not in retry_result:
                    retry_score = _quality_score(retry_result)
                    if retry_score > score:
                        result = retry_result

        # Strip internal _lines key before appending
        result.pop("_lines", None)
        results.append(result)

    doc.close()
    return results


# ---------------------------------------------------------------------------
#  Garbage collection for legacy uploaded media (images/PDFs)
# ---------------------------------------------------------------------------

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

GC_DIRS = [
    _HERMES_HOME / "media" / "uploads",
    Path("/tmp/hermes-media"),
]

GC_AGE_DAYS = 90
GC_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}


def _scan_media(age_days: int, dry_run: bool = True) -> dict:
    """List (or remove) media files older than age_days in GC_DIRS."""
    now = time.time()
    threshold = now - age_days * 86400

    found = 0
    candidates = []
    errors = []

    for d in GC_DIRS:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in GC_EXTS:
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError as e:
                errors.append(f"{f}: {e}")
                continue
            if mtime > threshold:
                continue
            found += 1
            age = (now - mtime) / 86400
            candidates.append({"path": str(f), "age_days": round(age, 1), "bytes": f.stat().st_size})

    if not dry_run:
        removed = 0
        freed = 0
        for c in candidates:
            try:
                Path(c["path"]).unlink()
                removed += 1
                freed += c["bytes"]
            except OSError as e:
                errors.append(f"rm {c['path']}: {e}")
        return {"removed": removed, "freed_bytes": freed, "errors": errors}

    return {"candidates": candidates, "count": found, "errors": errors}


# ---------------------------------------------------------------------------
#  Model pre-staging
# ---------------------------------------------------------------------------


def _cmd_fetch_models(copy_dir: str | None = None) -> None:
    """Pre-stage PaddleOCR models by triggering a dummy inference."""
    import shutil

    print("Initializing PaddleOCR and downloading models (if not cached)...")
    _load_paddle()

    import paddleocr

    paddle_dir = Path(paddleocr.__file__).resolve().parent
    models_root = Path.home() / ".paddlex" / "official_models"
    if not models_root.is_dir():
        models_root = paddle_dir / ".paddlex" / "official_models"

    if models_root.is_dir():
        models = sorted(models_root.iterdir())
        total = 0
        sizes = []
        for m in models:
            if not m.is_dir():
                continue
            sz = sum(f.stat().st_size for f in m.rglob("*") if f.is_file())
            total += sz
            sizes.append((m.name, sz))
        print(f"\nCached models ({models_root}):")
        for name, sz in sizes:
            print(f"  {name:35s} {sz // 1024:>6,d} KB")
        print(f"  {'─' * 42}")
        print(f"  {'Total':35s} {total // 1024:>6,d} KB")
    else:
        print(f"WARN  models directory not found: {models_root}")
        print("      Models may still be downloading. Try again after OCR runs.")

    if copy_dir:
        dst = Path(copy_dir)
        dst.mkdir(parents=True, exist_ok=True)
        if models_root.is_dir():
            for m in models_root.iterdir():
                if m.is_dir():
                    shutil.copytree(m, dst / m.name, dirs_exist_ok=True)
                    print(f"  Copied {m.name} -> {dst / m.name}")
            print(f"\nDone. {len(models)} model(s) copied to {dst}")
        else:
            print(f"ERROR  Nothing to copy \u2014 models not found at {models_root}")


# ---------------------------------------------------------------------------
#  Parallel batch processing
# ---------------------------------------------------------------------------


def _resolve_jobs(requested: int) -> int:
    """Clamp *requested* workers to a safe range.  0 = auto."""
    if requested > 0:
        return requested
    cpus = os.cpu_count() or 4
    return min(cpus, 4)


def _ocr_file(path: str, preprocess: str, denoise_strength: str, lang: str, retry: bool = False) -> list[dict]:
    """Run OCR on one file (image or PDF) and return a list of result dicts."""
    ext = Path(path).suffix.lower()
    if ext in _PDF_EXTENSIONS:
        return ocr_pdf(path, preprocess=preprocess, denoise_strength=denoise_strength, lang=lang, retry=retry)
    result = ocr_image(path, preprocess=preprocess, denoise_strength=denoise_strength, lang=lang, retry=retry)
    return [result]


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

_PDF_EXTENSIONS = {".pdf"}


def main():
    ap = argparse.ArgumentParser(description="Smart OCR + media GC")
    sub = ap.add_subparsers(dest="command", required=True)

    ocr_p = sub.add_parser("ocr", help="Run OCR on one or more images or PDFs")
    ocr_p.add_argument("paths", nargs="+", help="Image/PDF file(s) or glob pattern")
    ocr_p.add_argument("--format", choices=["text", "json"], default="text")
    ocr_p.add_argument("--preprocess", choices=["auto", "on", "off"], default="auto",
                       help="Preprocessing mode: auto (default, decide per image), "
                            "on (force all steps), off (skip all)")
    ocr_p.add_argument("--no-preprocess", action="store_true",
                       help="Disable preprocessing (shorthand for --preprocess off)")
    ocr_p.add_argument("--denoise", choices=["off", "light", "heavy"], default="light",
                       help="Denoising strength (default: light)")
    ocr_p.add_argument("--lang", default="ch",
                       help="PaddleOCR language code (default: ch). "
                            "Common: ch, en, japan, korean, chinese_cht")
    ocr_p.add_argument("--retry", action="store_true",
                       help="Re-run with opposite preprocessing if quality is low")
    ocr_p.add_argument("--jobs", type=int, default=0,
                       help="Parallel workers (0 = auto, 1 = sequential; default 0)")

    gc_p = sub.add_parser("gc", help="Media garbage collection")
    gc_p.add_argument("--scan", action="store_true", help="Dry-run (default)")
    gc_p.add_argument("--remove", action="store_true", help="Actually delete")
    gc_p.add_argument("--age", type=int, default=GC_AGE_DAYS, help=f"Age in days (default {GC_AGE_DAYS})")

    fm_p = sub.add_parser("fetch-models", help="Pre-stage PaddleOCR models")
    fm_p.add_argument("--dir", type=str, default=None, help="Copy cached models to this directory")

    args = ap.parse_args()

    if args.command == "ocr":
        pp = "off" if args.no_preprocess else args.preprocess
        ds = "light" if args.denoise == "light" else args.denoise
        lg = args.lang
        rt = getattr(args, "retry", False)
        max_workers = _resolve_jobs(getattr(args, "jobs", 0))
        import glob as glob_mod

        all_files = []
        for pattern in args.paths:
            all_files.extend(sorted(glob_mod.glob(pattern)))

        if not all_files:
            return

        if max_workers <= 1:
            # Sequential (original path)
            for path in all_files:
                for r in _ocr_file(path, pp, ds, lg, retry=rt):
                    _print_result(r, args.format)
        else:
            # Parallel batch
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                fut_to_path = {
                    pool.submit(_ocr_file, path, pp, ds, lg, retry=rt): path
                    for path in all_files
                }
                for fut in as_completed(fut_to_path):
                    path = fut_to_path[fut]
                    try:
                        results = fut.result()
                    except Exception as e:
                        _print_result({"filename": path, "error": str(e)}, args.format)
                        continue
                    for r in results:
                        _print_result(r, args.format)

    elif args.command == "fetch-models":
        _cmd_fetch_models(args.dir)

    elif args.command == "gc":
        dry = not args.remove
        if dry:
            res = _scan_media(args.age, dry_run=True)
            if res["errors"]:
                for e in res["errors"]:
                    print(e, file=sys.stderr)
            if not res["candidates"]:
                print("No eligible files found.")
                return
            print(f"{res['count']} file(s) older than {args.age} days:")
            for c in sorted(res["candidates"], key=lambda x: -x["age_days"]):
                print(f"  {c['path']}  ({c['age_days']}d, {c['bytes']}B)")
        else:
            res = _scan_media(args.age, dry_run=False)
            print(f"Removed {res['removed']} file(s), freed {res['freed_bytes']:,} bytes")
            if res["errors"]:
                for e in res["errors"]:
                    print(e, file=sys.stderr)

    else:
        ap.print_help()


def _print_result(result: dict, fmt: str) -> None:
    if fmt == "json":
        out = {k: v for k, v in result.items() if k != "_lines"}
        if "_preprocess_meta" in out:
            out["preprocess"] = out.pop("_preprocess_meta")
        print(json.dumps(out, ensure_ascii=False))
        return

    label = result.get("filename", "?")
    if "page_number" in result:
        label = f"{label} (p.{result['page_number']})"
    if "error" in result:
        print(f"--- {label} ---")
        print(f"ERROR: {result['error']}")
        print()
        return

    print(f"--- {label} ---")
    print(f"Layout: {result.get('layout', '?')}")
    for p in result.get("paragraphs", []):
        print(p)
    print()


if __name__ == "__main__":
    main()
