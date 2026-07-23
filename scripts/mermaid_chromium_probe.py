#!/usr/bin/env python3
"""Bounded Chromium feasibility probe for the offline Mermaid renderer."""

from __future__ import annotations

import argparse
import json
import subprocess
import shutil
import tempfile
from pathlib import Path


SAFE_PREFIX = (
    "--disable-gpu",
    "--run-all-compositor-stages-before-draw",
    "--enable-logging=stderr",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
    "--dump-dom",
)


def _html(asset_name: str | None) -> str:
    script = f'<script src="{asset_name}"></script>' if asset_name else ""
    return (
        "<!doctype html><html><head>"
        "<meta charset=\"utf-8\">"
        "<meta http-equiv=\"Content-Security-Policy\" content=\""
        "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
        "img-src data:; font-src data:; connect-src 'none'; object-src 'none'; "
        "frame-src 'none'; base-uri 'none'; form-action 'none'\">"
        f"</head><body data-probe=\"ok\"><div id=\"probe\">ok</div>{script}</body></html>"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chromium", default="/snap/bin/chromium")
    parser.add_argument("--asset", type=Path)
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    if args.asset and not args.asset.is_file():
        raise SystemExit(f"asset not found: {args.asset}")

    with tempfile.TemporaryDirectory(
        prefix="hermes-mermaid-probe-",
        dir=str(args.temp_dir) if args.temp_dir else None,
    ) as tmp:
        root = Path(tmp)
        html = root / "probe.html"
        asset_name = None
        if args.asset:
            local_asset = root / "mermaid.min.js"
            shutil.copy2(args.asset, local_asset)
            (root / "probe.js").write_text(
                "if (typeof mermaid !== 'undefined') { "
                "mermaid.initialize({startOnLoad:false,securityLevel:'strict'}); "
                "document.documentElement.dataset.mermaidAsset='loaded'; }",
                encoding="utf-8",
            )
            asset_name = "mermaid.min.js"
            html.write_text(_html(asset_name) + '<script src="probe.js"></script>', encoding="utf-8")
        else:
            html.write_text(_html(None), encoding="utf-8")
        base = [
            args.chromium,
            "--headless=new",
            *SAFE_PREFIX,
            html.as_uri(),
        ]
        attempts = [base, [args.chromium, "--headless", *SAFE_PREFIX, html.as_uri()]]
        result = None
        for command in attempts:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                print(json.dumps({"ok": False, "error": type(exc).__name__}))
                return 1
            if result.returncode == 0:
                break

        assert result is not None
        payload = {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout_has_probe": "id=\"probe\"" in result.stdout,
            "asset_loaded": 'data-mermaid-asset="loaded"' in result.stdout,
            "stdout_tail": result.stdout[-4000:],
            "asset_mode": bool(args.asset),
            "stderr": result.stderr[-4000:],
            "command": result.args,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["ok"] and payload["stdout_has_probe"] and (not args.asset or payload["asset_loaded"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
