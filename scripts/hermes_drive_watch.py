#!/usr/bin/env python3
"""Drive folder watcher for Hermes.

Polls a configured Google Drive folder, sends newly observed files to DocuBot
using the shared ingestion call helper, and persists state so each run is
incremental.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Reuse OAuth setup from the existing Google Workspace skill (no reimplementation).
_SCRIPT_DIR = Path(__file__).resolve().parent
_HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
_GWS_DIR = _HERMES_HOME / "skills/productivity/google-workspace/scripts"

if str(_GWS_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(_GWS_DIR))

from google_api import build_service  # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parents[1]
if not (_REPO_ROOT / "plugins/platforms/telegram/docubot_mcp_gateway.py").is_file():
    _REPO_ROOT = Path.home() / ".hermes" / "hermes-agent"

if str(_REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(_REPO_ROOT))

from plugins.platforms.telegram.docubot_mcp_gateway import ingest_document_to_docubot  # noqa: E402


STATE_DIR = _HERMES_HOME / "cron"
STATE_PATH = STATE_DIR / "drive_watch_state.json"
STATE_MAX_IDS = 5000
PAGE_SIZE = 100
DRY_RUN = os.getenv("DRIVE_WATCH_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}

NATIVE_EXPORT = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}


def _safe_name(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "document"
    value = re.sub(r"[\\x00-\\x1f\\\\/\\\\*?:\"<>|]", "_", value)
    return value[:150] or "document"


def _to_iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"last_seen_time": None, "processed_file_ids": [], "folder_id": None}

    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"last_seen_time": None, "processed_file_ids": [], "folder_id": None}

    if not isinstance(payload, dict):
        return {"last_seen_time": None, "processed_file_ids": [], "folder_id": None}

    ids = [str(v) for v in payload.get("processed_file_ids", []) if str(v)]
    return {
        "last_seen_time": payload.get("last_seen_time"),
        "processed_file_ids": list(dict.fromkeys(ids))[:STATE_MAX_IDS],
        "folder_id": payload.get("folder_id"),
    }


def _save_state(state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_folder_id() -> str | None:
    value = os.getenv("DRIVE_WATCH_FOLDER_ID", "").strip()
    if value:
        return value

    cfg_path = _HERMES_HOME / "config.yaml"
    if not cfg_path.exists():
        return None

    try:
        import yaml

        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        folder_id = cfg.get("drive_watch", {}).get("folder_id")
        if isinstance(folder_id, str) and folder_id.strip():
            return folder_id.strip()
    except Exception:
        return None
    return None


def _list_changed_files(service, folder_id: str, since: str | None) -> List[Dict[str, Any]]:
    query = [
        f"'{folder_id}' in parents",
        "trashed = false",
        "mimeType != 'application/vnd.google-apps.folder'",
    ]
    if since:
        query.append(f"modifiedTime > '{since}'")
    query_text = " and ".join(query)

    found: List[Dict[str, Any]] = []
    page_token = None
    while True:
        response = service.files().list(
            q=query_text,
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink)",
            pageToken=page_token,
            orderBy="modifiedTime asc",
            pageSize=PAGE_SIZE,
        ).execute()
        found.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return found


def _download_file(service, item: Dict[str, Any], target_dir: Path) -> Path:
    try:
        from googleapiclient.http import MediaIoBaseDownload
    except Exception:
        raise RuntimeError("googleapiclient is required for Drive downloads")

    file_id = item["id"]
    name = _safe_name(item.get("name") or file_id)
    mime = item.get("mimeType") or ""

    if mime in NATIVE_EXPORT:
        export_mime, suffix = NATIVE_EXPORT[mime]
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
        out_name = f"{name}{suffix}"
    else:
        request = service.files().get_media(fileId=file_id)
        out_name = name

    out_path = target_dir / out_name
    with out_path.open("wb") as handle:
        downloader = MediaIoBaseDownload(handle, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return out_path


def main() -> int:
    folder_id = _resolve_folder_id()
    if not folder_id:
        raise SystemExit("DRIVE_WATCH_FOLDER_ID is required (env var or config drive_watch.folder_id).")

    service = build_service("drive", "v3")

    state = _load_state()
    if state.get("folder_id") and state.get("folder_id") != folder_id:
        state = {"last_seen_time": None, "processed_file_ids": [], "folder_id": folder_id}

    state.setdefault("processed_file_ids", [])
    state["folder_id"] = folder_id
    state_ids = set(str(v) for v in state.get("processed_file_ids", []))
    last_seen = _parse_iso(state.get("last_seen_time") or "")

    files = _list_changed_files(service, folder_id, _to_iso_z(last_seen) if last_seen else None)
    if not files:
        print("no new files")
        return 0

    cache_dir = STATE_DIR / "drive_watch_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    max_seen = last_seen
    processed = 0
    for item in files:
        file_id = item.get("id")
        if not file_id:
            continue
        modified = _parse_iso(item.get("modifiedTime") or "")
        if not modified:
            continue

        if file_id in state_ids and (max_seen is None or modified <= max_seen):
            continue

        if max_seen is None or modified > max_seen:
            max_seen = modified

        downloaded = _download_file(service, item, cache_dir)
        if DRY_RUN:
            print(f"DRY-RUN: would ingest {downloaded.name} ({file_id})")
            if downloaded.exists():
                try:
                    downloaded.unlink()
                except Exception:
                    pass
            continue

        result = ingest_document_to_docubot(
            source="drive-watch",
            action="folder-watch",
            local_path=str(downloaded),
            stable_key=file_id,
            metadata={
                "platform": "google-drive",
                "folder_id": folder_id,
                "file_id": file_id,
                "file_name": item.get("name"),
                "mime_type": item.get("mimeType"),
                "web_view_link": item.get("webViewLink"),
            },
        )
        if result.get("status") != "error":
            state_ids.add(file_id)
            processed += 1

        if result.get("status") == "error":
            print(f"failed ingest {file_id}: {result.get('error')}")

        try:
            downloaded.unlink()
        except Exception:
            pass

    state["processed_file_ids"] = list(dict.fromkeys(state_ids))[-STATE_MAX_IDS:]
    if max_seen:
        state["last_seen_time"] = _to_iso_z(max_seen)
    _save_state(state)

    print(f"processed={processed} scanned={len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
