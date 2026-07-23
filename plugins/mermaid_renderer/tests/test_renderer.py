import os
import stat
from pathlib import Path

from PIL import Image

from plugins.mermaid_renderer import renderer


def test_rejects_unsafe_inputs(tmp_path, monkeypatch):
    root = tmp_path / "media"
    monkeypatch.setattr(renderer, "MEDIA_ROOT", root)
    output = root / "safe.png"
    for source in ("<script>x</script>", "flowchart TD; A --> click A", "A href https://x"):
        result = renderer.render_mermaid_to_png(source, output, chromium_path=Path("missing"))
        assert not result.success
        assert result.error_code == "unsafe_source"


def test_rejects_paths_and_dimensions(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    monkeypatch.setattr(renderer, "MEDIA_ROOT", root)
    safe = "flowchart TD\n A-->B"
    assert renderer.render_mermaid_to_png(safe, root / "../escape.png").error_code == "output_outside_media_root"
    assert renderer.render_mermaid_to_png(safe, root / "bad name.png").error_code == "output_outside_media_root"
    assert renderer.render_mermaid_to_png(safe, root / "x.png", width=0).error_code == "invalid_dimensions"


def test_html_has_strict_csp_and_security_level(monkeypatch, tmp_path):
    monkeypatch.setattr(renderer, "MEDIA_ROOT", tmp_path / "media")
    captured = {}

    class Proc:
        returncode = 1
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        html_path = Path(command[-1].removeprefix("file://"))
        captured["html"] = html_path.read_text()
        captured["bootstrap"] = html_path.with_name("bootstrap.js").read_text()
        return Proc()

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    result = renderer.render_mermaid_to_png("flowchart TD\n A-->B", tmp_path / "media" / "x.png")
    assert not result.success
    assert "--no-sandbox" not in captured["command"]
    assert "Content-Security-Policy" in captured["html"]
    assert "script-src 'self'" in captured["html"]
    assert "securityLevel:'strict'" in captured["bootstrap"]
    assert "--disable-background-networking" in captured["command"]
    assert "--no-sandbox" not in captured["command"]
    assert "--screenshot=" in " ".join(captured["command"])


def test_bootstrap_fits_rendered_svg_to_the_png_canvas(monkeypatch, tmp_path):
    root = tmp_path / "media"
    staging_root = tmp_path / "stage"
    monkeypatch.setattr(renderer, "MEDIA_ROOT", root)
    monkeypatch.setattr(renderer, "STAGING_ROOT", staging_root, raising=False)
    captured = {}

    class Proc:
        returncode = 1
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        html_path = Path(command[-1].removeprefix("file://"))
        captured["html"] = html_path.read_text()
        captured["bootstrap"] = html_path.with_name("bootstrap.js").read_text()
        return Proc()

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    result = renderer.render_mermaid_to_png("flowchart TD\n A-->B", root / "x.png")

    assert result.error_code == "chromium_failed"
    assert "display:flex" in captured["html"]
    assert "getBoundingClientRect()" in captured["bootstrap"]
    assert "Math.min(" in captured["bootstrap"]
    assert "mermaidReady='1'" in captured["bootstrap"]


def test_content_bbox_uses_threshold_and_composites_rgba_to_white():
    near_white = Image.new("RGB", (100, 100), "white")
    near_white.putpixel((10, 10), (250, 250, 250))
    assert renderer._content_bbox(near_white) is None

    near_white.putpixel((50, 60), (249, 255, 255))
    assert renderer._content_bbox(near_white) == (50, 60, 51, 61)

    transparent = Image.new("RGBA", (20, 20), (255, 255, 255, 0))
    transparent.putpixel((2, 2), (0, 0, 0, 1))
    assert renderer._content_bbox(transparent) is None
    transparent.putpixel((4, 5), (0, 0, 0, 255))
    assert renderer._content_bbox(transparent) == (4, 5, 5, 6)


def test_crop_staged_png_applies_padding_and_requested_bounds(tmp_path):
    wide = tmp_path / "wide.png"
    image = Image.new("RGB", (1600, 1000), "white")
    image.paste((0, 0, 0), (200, 400, 1400, 600))
    image.save(wide)

    wide_result = renderer._crop_staged_png(wide, requested_width=1600, requested_height=1000)

    assert wide_result.success
    assert (wide_result.width, wide_result.height) == (1328, 328)
    with Image.open(wide) as cropped:
        assert cropped.size == (1328, 328)

    tall = tmp_path / "tall.png"
    image = Image.new("RGB", (1600, 1000), "white")
    image.paste((0, 0, 0), (700, 100, 900, 900))
    image.save(tall)

    tall_result = renderer._crop_staged_png(tall, requested_width=1600, requested_height=1000)

    assert tall_result.success
    assert (tall_result.width, tall_result.height) == (328, 928)
    with Image.open(tall) as cropped:
        assert cropped.size == (328, 928)


def test_crop_staged_png_honors_effective_minimum_without_exceeding_requested(tmp_path):
    output = tmp_path / "small.png"
    image = Image.new("RGB", (200, 100), "white")
    image.paste((0, 0, 0), (95, 45, 105, 55))
    image.save(output)

    result = renderer._crop_staged_png(output, requested_width=200, requested_height=100)

    assert result.success
    assert result.width <= 200
    assert result.height <= 100
    assert result.width >= min(renderer.MIN_OUTPUT_WIDTH, 200)
    assert result.height >= min(renderer.MIN_OUTPUT_HEIGHT, 100)


def test_crop_staged_png_rejects_empty_render(tmp_path):
    output = tmp_path / "empty.png"
    Image.new("RGB", (1600, 1000), "white").save(output)

    result = renderer._crop_staged_png(output, requested_width=1600, requested_height=1000)

    assert not result.success
    assert result.error_code == "empty_render"
    assert output.exists()


def test_timeout_is_bounded_and_temp_is_cleaned(monkeypatch, tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    staging_root = tmp_path / "stage"
    monkeypatch.setattr(renderer, "MEDIA_ROOT", root)
    monkeypatch.setattr(renderer, "STAGING_ROOT", staging_root, raising=False)

    def fail_run(*args, **kwargs):
        raise renderer.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(renderer.subprocess, "run", fail_run)
    result = renderer.render_mermaid_to_png("flowchart TD\n A-->B", root / "x.png", timeout_seconds=0.1)
    assert result.error_code == "chromium_timeout"
    assert not list(staging_root.iterdir())


def test_stages_valid_png_then_atomically_moves_to_secure_final_root(monkeypatch, tmp_path):
    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    os.chmod(final_root, 0o700)
    staging_root = tmp_path / "stage"
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    monkeypatch.setattr(renderer, "STAGING_ROOT", staging_root, raising=False)
    captured = {}

    class Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        target = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--screenshot=")))
        captured["target"] = target
        Image.new("RGB", (1, 1), "black").save(target)
        return Proc()

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    output = final_root / "result.png"
    result = renderer.render_mermaid_to_png("flowchart TD\n A-->B", output, width=1, height=1)

    assert result.success
    assert result.output_path == output
    assert output.is_file()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert captured["target"].parent.parent == staging_root
    assert not any(staging_root.iterdir())


def test_empty_render_releases_reservation_and_staging(monkeypatch, tmp_path):
    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    os.chmod(final_root, 0o700)
    staging_root = tmp_path / "stage"
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    monkeypatch.setattr(renderer, "STAGING_ROOT", staging_root, raising=False)

    class Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        target = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--screenshot=")))
        Image.new("RGB", (320, 240), "white").save(target)
        return Proc()

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    output = final_root / "result.png"
    result = renderer.render_mermaid_to_png("flowchart TD\n A-->B", output, width=320, height=240)

    assert result.error_code == "empty_render"
    assert not output.exists()
    assert not any(staging_root.iterdir())


def test_rejects_insecure_final_root_before_running_chromium(monkeypatch, tmp_path):
    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o755)
    os.chmod(final_root, 0o755)
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    result = renderer.render_mermaid_to_png("flowchart TD\n A-->B", final_root / "result.png")
    assert result.error_code == "media_root_insecure"


def test_rejects_corrupt_staged_png_and_removes_reservation(monkeypatch, tmp_path):
    final_root = tmp_path / "media"
    final_root.mkdir(mode=0o700)
    os.chmod(final_root, 0o700)
    staging_root = tmp_path / "stage"
    monkeypatch.setattr(renderer, "MEDIA_ROOT", final_root)
    monkeypatch.setattr(renderer, "STAGING_ROOT", staging_root, raising=False)

    class Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(command, **kwargs):
        target = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("--screenshot=")))
        target.write_bytes(b"\x89PNG\r\n\x1a\ntruncated")
        return Proc()

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    output = final_root / "result.png"
    result = renderer.render_mermaid_to_png("flowchart TD\n A-->B", output, width=1, height=1)

    assert result.error_code == "invalid_png"
    assert not output.exists()
    assert not any(staging_root.iterdir())
