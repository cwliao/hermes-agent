"""``hermes smartocr`` handler — delegates to the PaddleOCR helper script."""

from __future__ import annotations

import sys
from pathlib import Path

HELPER = (
    Path(__file__).resolve().parent.parent
    / "optional-skills"
    / "productivity"
    / "smart-ocr"
    / "scripts"
    / "paddle_ocr_helper.py"
)


def run_smartocr(args) -> None:
    sub = getattr(args, "smartocr_command", None)

    if sub == "fetch-models":
        cmd = ["fetch-models"]
        if getattr(args, "dir", None):
            cmd.extend(["--dir", args.dir])
        _delegate(cmd)
        return

    if sub in ("verify", "doctor"):
        _cmd_verify()
        return

    if not HELPER.is_file():
        print(
            f"Error: Smart OCR helper not found at {HELPER}",
            file=sys.stderr,
        )
        print("Install the skill: hermes skills install official/productivity/smart-ocr")
        sys.exit(1)

    if sub == "ocr":
        cmd = ["ocr", *args.paths, "--format", getattr(args, "format", "text")]
        pp = getattr(args, "preprocess", "auto")
        if getattr(args, "no_preprocess", False):
            pp = "off"
        cmd.extend(["--preprocess", pp])
        if getattr(args, "retry", False):
            cmd.append("--retry")
        if getattr(args, "lang", None):
            cmd.extend(["--lang", args.lang])
        jobs = getattr(args, "jobs", 0)
        if jobs:
            cmd.extend(["--jobs", str(jobs)])
        _delegate(cmd)
    elif sub == "gc":
        cmd = ["gc"]
        if args.remove:
            cmd.append("--remove")
        if args.scan:
            cmd.append("--scan")
        cmd.extend(["--age", str(getattr(args, "age", 90))])
        _delegate(cmd)
    else:
        print(f"Unknown smartocr subcommand: {sub}", file=sys.stderr)
        sys.exit(2)


def _delegate(cli_args: list[str]) -> None:
    import subprocess

    proc = subprocess.run([sys.executable, str(HELPER), *cli_args])
    sys.exit(proc.returncode)


def _cmd_verify() -> None:
    """Check whether the helper is importable and PaddleOCR is installed."""
    try:
        import importlib.util as iu

        spec = iu.spec_from_file_location("paddle_ocr_helper", HELPER)
        if spec is None or spec.loader is None:
            print("FAIL  Cannot load paddle_ocr_helper.py")
            sys.exit(1)

        mod = iu.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Check PaddleOCR can be imported (but don't force model download)
        try:
            import paddleocr  # noqa: F401
        except ImportError:
            print("FAIL  PaddleOCR is not installed.")
            print("       Run: pip install paddlepaddle pillow opencv-python-headless")
            sys.exit(1)

        try:
            import pypdfium2  # noqa: F401
            pdf_ok = True
        except ImportError:
            pdf_ok = False

        print("OK  Smart OCR helper is loadable.")
        print("OK  PaddleOCR is installed.")
        if pdf_ok:
            print("OK  pypdfium2 is installed (PDF support).")
        else:
            print("WARN  pypdfium2 not found — PDF OCR disabled.")
            print("      Run: pip install pypdfium2")
        print("")
        print("Run `hermes smartocr ocr <image-or-pdf>`.")
    except Exception as e:
        print(f"FAIL  {e}")
        sys.exit(1)
