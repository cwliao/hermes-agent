"""Offline, strictly bounded Mermaid-to-PNG renderer skeleton.

This module is intentionally not registered as a Hermes plugin in Gate 2.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError


MEDIA_ROOT = Path("/home/cwliao/.hermes/media/mermaid-renderer")
STAGING_ROOT = Path("/home/cwliao/snap/chromium/common/hermes-mermaid-stage")
ASSET = Path(__file__).with_name("assets") / "mermaid.min.js"
MAX_SOURCE = 100_000
MAX_OUTPUT_BYTES = 20 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CONTENT_PADDING = 64
BACKGROUND_THRESHOLD = 250
MIN_OUTPUT_WIDTH = 320
MIN_OUTPUT_HEIGHT = 240
FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.png$")
FORBIDDEN = re.compile(
    r"<\s*(?:script|iframe|img|object|embed)\b|\bhref\b|javascript\s*:|"
    r"\bclick\b|\bcallback\b|(?:https?|file|data)\s*://",
    re.IGNORECASE,
)
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; object-src 'none'; "
    "frame-src 'none'; base-uri 'none'; form-action 'none';"
)


@dataclass(frozen=True)
class RenderResult:
    success: bool
    output_path: Path
    bytes_written: int = 0
    error_code: str | None = None


@dataclass(frozen=True)
class CropResult:
    success: bool
    width: int = 0
    height: int = 0
    error_code: str | None = None


def _validate(source: str, output: Path, width: int, height: int) -> str | None:
    if not isinstance(source, str) or not source.strip():
        return "empty_source"
    if len(source) > MAX_SOURCE:
        return "source_too_large"
    if FORBIDDEN.search(source):
        return "unsafe_source"
    if not (1 <= width <= 4096 and 1 <= height <= 4096):
        return "invalid_dimensions"
    root = MEDIA_ROOT.resolve()
    try:
        resolved = output.resolve()
    except OSError:
        return "invalid_output_path"
    if resolved.parent != root or not FILENAME.fullmatch(output.name):
        return "output_outside_media_root"
    if output.is_symlink():
        return "output_symlink"
    if output.exists():
        return "output_exists"
    return None


def _prepare_secure_directory(path: Path, error_code: str) -> str | None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry = path.lstat()
    except OSError:
        return error_code
    if not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
        return error_code
    if entry.st_uid != os.getuid() or stat.S_IMODE(entry.st_mode) != 0o700:
        return error_code
    return None


def _validate_staged_png(
    path: Path,
    width: int,
    height: int,
    *,
    exact_dimensions: bool,
) -> str | None:
    try:
        entry = path.lstat()
    except OSError:
        return "png_missing"
    if not stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
        return "invalid_png"
    if entry.st_size <= 0 or entry.st_size > MAX_OUTPUT_BYTES:
        return "invalid_png"
    try:
        with path.open("rb") as source:
            if source.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
                return "invalid_png"
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            if image.format != "PNG":
                return "invalid_png"
            if exact_dimensions and image.size != (width, height):
                return "invalid_png"
            if not exact_dimensions:
                minimum_width = min(MIN_OUTPUT_WIDTH, width)
                minimum_height = min(MIN_OUTPUT_HEIGHT, height)
                if not (
                    minimum_width <= image.width <= width
                    and minimum_height <= image.height <= height
                ):
                    return "invalid_png"
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return "invalid_png"
    return None


def _content_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Return the thresholded non-white bounds after compositing on white."""
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    flattened = Image.alpha_composite(background, rgba).convert("RGB")
    mask = flattened.point(lambda channel: 0 if channel >= BACKGROUND_THRESHOLD else 255)
    return mask.getbbox()


def _expand_axis(start: int, end: int, limit: int, minimum: int) -> tuple[int, int]:
    target = min(limit, max(end - start, minimum))
    missing = target - (end - start)
    start = max(0, start - missing // 2)
    end = min(limit, end + missing - missing // 2)
    if end - start < target:
        if start == 0:
            end = min(limit, target)
        else:
            start = max(0, end - target)
    return start, end


def _crop_staged_png(path: Path, *, requested_width: int, requested_height: int) -> CropResult:
    """Crop a valid staged PNG to thresholded content plus bounded padding."""
    cropped_path = path.with_name("cropped.png")
    try:
        with Image.open(path) as image:
            image.load()
            bbox = _content_bbox(image)
            if bbox is None:
                return CropResult(False, error_code="empty_render")
            left = max(0, bbox[0] - CONTENT_PADDING)
            top = max(0, bbox[1] - CONTENT_PADDING)
            right = min(image.width, bbox[2] + CONTENT_PADDING)
            bottom = min(image.height, bbox[3] + CONTENT_PADDING)
            left, right = _expand_axis(
                left,
                right,
                image.width,
                min(MIN_OUTPUT_WIDTH, requested_width),
            )
            top, bottom = _expand_axis(
                top,
                bottom,
                image.height,
                min(MIN_OUTPUT_HEIGHT, requested_height),
            )
            cropped = image.crop((left, top, right, bottom))
            try:
                cropped.save(cropped_path, format="PNG")
            finally:
                cropped.close()
        os.replace(cropped_path, path)
        return CropResult(True, right - left, bottom - top)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return CropResult(False, error_code="png_crop_failed")
    finally:
        try:
            cropped_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _reserve_destination(path: Path) -> int | None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return None
    except OSError:
        return None
    os.close(descriptor)
    try:
        return path.lstat().st_ino
    except OSError:
        return None


def _release_reservation(path: Path, inode: int | None) -> None:
    if inode is None:
        return
    try:
        if path.lstat().st_ino == inode:
            path.unlink()
    except OSError:
        pass


def _html() -> str:
    return f'''<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{CSP}">
<style>html,body{{margin:0;width:100%;height:100%;background:#fff}}#mermaid-root{{width:100%;height:100%;display:flex;align-items:center;justify-content:center;overflow:hidden}}</style>
</head><body><div id="mermaid-root"></div>
<script src="mermaid.min.js"></script><script src="bootstrap.js"></script>
</body></html>'''


def render_mermaid_to_png(
    mermaid_source: str,
    output_path: Path,
    *,
    width: int = 1600,
    height: int = 1000,
    chromium_path: Path = Path("/snap/bin/chromium"),
    timeout_seconds: float = 30.0,
) -> RenderResult:
    output_path = Path(output_path)
    error = _validate(mermaid_source, output_path, width, height)
    if error:
        return RenderResult(False, output_path, error_code=error)
    if not ASSET.is_file():
        return RenderResult(False, output_path, error_code="asset_missing")
    error = _prepare_secure_directory(MEDIA_ROOT, "media_root_insecure")
    if error:
        return RenderResult(False, output_path, error_code=error)
    error = _prepare_secure_directory(STAGING_ROOT, "staging_root_insecure")
    if error:
        return RenderResult(False, output_path, error_code=error)
    try:
        if MEDIA_ROOT.stat().st_dev != STAGING_ROOT.stat().st_dev:
            return RenderResult(False, output_path, error_code="cross_filesystem")
    except OSError:
        return RenderResult(False, output_path, error_code="cross_filesystem")
    reservation = _reserve_destination(output_path)
    if reservation is None:
        return RenderResult(False, output_path, error_code="output_exists")
    moved = False
    try:
        with tempfile.TemporaryDirectory(prefix="render-", dir=str(STAGING_ROOT)) as tmp:
            root = Path(tmp)
            staged_output = root / "output.png"
            shutil.copy2(ASSET, root / "mermaid.min.js")
            bootstrap = (
                "const source = " + json.dumps(mermaid_source) + ";\n"
                "mermaid.initialize({startOnLoad:false,securityLevel:'strict'});\n"
                "const padding = 64;\n"
                "function fitSvg(svg) {"
                "const box = svg.getBoundingClientRect();"
                "if (box.width <= 0 || box.height <= 0) throw new Error('invalid_svg_size');"
                "const availableWidth = Math.max(1, window.innerWidth - padding * 2);"
                "const availableHeight = Math.max(1, window.innerHeight - padding * 2);"
                "const scale = Math.min(availableWidth / box.width, availableHeight / box.height);"
                "svg.style.maxWidth = 'none';svg.style.maxHeight = 'none';svg.style.display = 'block';"
                "svg.setAttribute('width', String(Math.floor(box.width * scale)));"
                "svg.setAttribute('height', String(Math.floor(box.height * scale)));"
                "}\n"
                "mermaid.render('mermaid-svg', source).then(({svg}) => {"
                "const root=document.getElementById('mermaid-root');root.innerHTML=svg;"
                "const rendered=root.querySelector('svg');if(!rendered)throw new Error('missing_svg');"
                "fitSvg(rendered);document.documentElement.dataset.mermaidReady='1';})"
                ".catch(() => {document.documentElement.dataset.mermaidReady='0';});\n"
            )
            (root / "bootstrap.js").write_text(bootstrap, encoding="utf-8")
            html = root / "render.html"
            html.write_text(_html(), encoding="utf-8")
            command = [
                str(chromium_path), "--headless=new", "--disable-gpu",
                "--run-all-compositor-stages-before-draw", "--enable-logging=stderr",
                "--disable-background-networking", "--disable-component-update",
                "--disable-default-apps", "--disable-sync", "--no-first-run",
                "--virtual-time-budget=5000", f"--window-size={width},{height}",
                f"--screenshot={staged_output}", html.as_uri(),
            ]
            try:
                completed = subprocess.run(command, capture_output=True, text=True,
                                           timeout=timeout_seconds, check=False)
            except subprocess.TimeoutExpired:
                return RenderResult(False, output_path, error_code="chromium_timeout")
            except OSError:
                return RenderResult(False, output_path, error_code="chromium_unavailable")
            if completed.returncode != 0:
                return RenderResult(False, output_path, error_code="chromium_failed")
            error = _validate_staged_png(staged_output, width, height, exact_dimensions=True)
            if error:
                return RenderResult(False, output_path, error_code=error)
            crop = _crop_staged_png(
                staged_output,
                requested_width=width,
                requested_height=height,
            )
            if not crop.success:
                return RenderResult(False, output_path, error_code=crop.error_code or "png_crop_failed")
            error = _validate_staged_png(staged_output, width, height, exact_dimensions=False)
            if error:
                return RenderResult(False, output_path, error_code=error)
            try:
                os.chmod(staged_output, 0o600)
                os.replace(staged_output, output_path)
            except OSError:
                return RenderResult(False, output_path, error_code="atomic_move_failed")
            moved = True
            return RenderResult(True, output_path, bytes_written=output_path.stat().st_size)
    finally:
        if not moved:
            _release_reservation(output_path, reservation)
