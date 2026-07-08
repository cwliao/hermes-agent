"""``hermes smartocr`` subcommand parser.

Delegates to the Smart OCR helper script for CJK newspaper OCR
and media garbage collection.
"""

from __future__ import annotations

from typing import Callable


def build_smartocr_parser(subparsers, *, cmd_smartocr: Callable) -> None:
    """Attach the ``smartocr`` subcommand to ``subparsers``."""
    p = subparsers.add_parser(
        "smartocr",
        help="Smart OCR — CJK newspaper/document OCR + media GC",
        description="CJK newspaper/document OCR via PaddleOCR with RTL column-aware "
        "reading order, plus garbage collection for uploaded media.",
    )
    p.set_defaults(func=cmd_smartocr)
    sub = p.add_subparsers(dest="smartocr_command", required=True)

    # ----- ocr -----
    ocr_p = sub.add_parser("ocr", help="Run OCR on one or more images")
    ocr_p.add_argument("paths", nargs="+", help="Image file(s) or glob pattern")
    ocr_p.add_argument("--format", choices=["text", "json"], default="text")
    ocr_p.add_argument("--preprocess", choices=["auto", "on", "off"], default="auto",
                       help="Preprocessing mode (default: auto)")
    ocr_p.add_argument("--no-preprocess", action="store_true",
                       help="Disable preprocessing (shorthand for --preprocess off)")
    ocr_p.add_argument("--lang", default="ch",
                       help="PaddleOCR language code (default: ch). "
                            "Common: ch, en, japan, korean, chinese_cht")
    ocr_p.add_argument("--retry", action="store_true",
                       help="Re-run with opposite preprocessing if quality is low")
    ocr_p.add_argument("--jobs", type=int, default=0,
                       help="Parallel workers (0 = auto, 1 = sequential; default 0)")

    # ----- gc -----
    gc_p = sub.add_parser("gc", help="Media garbage collection")
    gc_p.add_argument("--scan", action="store_true", help="Dry-run (default)")
    gc_p.add_argument("--remove", action="store_true", help="Actually delete")
    gc_p.add_argument(
        "--age", type=int, default=90, help="Age in days (default 90)"
    )

    # ----- fetch-models -----
    fm_p = sub.add_parser("fetch-models", help="Pre-stage PaddleOCR models")
    fm_p.add_argument("--dir", type=str, default=None,
                      help="Copy cached models to this directory for offline transfer")

    # ----- doctor (alias: verify) -----
    sub.add_parser("doctor", aliases=["verify"], help="Check Smart OCR environment")
