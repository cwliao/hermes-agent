"""Wire-contract tests for the Drive-watch -> DocuBot upload path."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


def _load_gateway_module():
    module_path = (
        Path(__file__).parents[1]
        / "plugins"
        / "platforms"
        / "telegram"
        / "docubot_mcp_gateway.py"
    )
    module_name = "docubot_mcp_gateway_contract_test"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


gateway = _load_gateway_module()


class _Response:
    text = '{"job_id":"job-1"}'
    status_code = 202
    ok = True

    def json(self):
        return {"job_id": "job-1"}


class _Session:
    def __init__(self):
        self.post_kwargs = None
        self.uploaded_bytes = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, _endpoint, **kwargs):
        self.post_kwargs = kwargs
        self.uploaded_bytes = kwargs["files"]["file"][1].read()
        return _Response()


def test_multipart_upload_matches_docubot_form_and_file_contract(monkeypatch, tmp_path):
    upload = tmp_path / "quarterly-report.pdf"
    upload.write_bytes(b"%PDF-test-content")
    session = _Session()
    monkeypatch.setenv("DOCUBOT_INGEST_URL", "https://docubot.test/jobs/upload")
    monkeypatch.setattr(gateway, "Session", lambda: session)

    metadata = {
        "platform": "google-drive",
        "folder_id": "folder-1",
        "file_id": "file-1",
        "file_name": "quarterly-report.pdf",
        "mime_type": "application/pdf",
        "web_view_link": "https://drive.test/file-1",
    }
    result = gateway.ingest_document_to_docubot(
        source="drive-watch",
        action="document_review",
        metadata=metadata,
        local_path=str(upload),
        stable_key="file-1",
        multipart=True,
        timeout_sec=7,
    )

    assert result["http_status"] == 202
    assert session.uploaded_bytes == b"%PDF-test-content"
    assert session.post_kwargs["data"] == {
        "action": "document_review",
        "source_system": "drive-watch",
        "purpose_hint": json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    }
    assert set(session.post_kwargs["data"]) == {
        "action",
        "source_system",
        "purpose_hint",
    }
    filename, file_object, content_type = session.post_kwargs["files"]["file"]
    assert filename == "quarterly-report.pdf"
    assert file_object.closed is True
    assert content_type == "application/pdf"
    assert session.post_kwargs["headers"] == {
        "Idempotency-Key": "drive-watch-document_review-file-1"
    }
    assert session.post_kwargs["timeout"] == 7
    assert "json" not in session.post_kwargs


def test_multipart_omits_optional_form_fields_when_metadata_is_none(monkeypatch, tmp_path):
    upload = tmp_path / "scan.png"
    upload.write_bytes(b"png-bytes")
    session = _Session()
    monkeypatch.setenv("DOCUBOT_INGEST_URL", "https://docubot.test/jobs/upload")
    monkeypatch.setattr(gateway, "Session", lambda: session)

    gateway.ingest_document_to_docubot(
        source="drive-watch",
        action="document_review",
        metadata=None,
        local_path=str(upload),
        stable_key="file-2",
        multipart=True,
    )

    assert session.post_kwargs["data"] == {
        "action": "document_review",
        "source_system": "drive-watch",
    }
    assert "purpose_hint" not in session.post_kwargs["data"]
    assert session.uploaded_bytes == b"png-bytes"
    assert session.post_kwargs["files"]["file"][2] == "image/png"


def test_json_mode_uses_docubot_source_system_field(monkeypatch, tmp_path):
    upload = tmp_path / "report.pdf"
    upload.write_bytes(b"bytes")
    session = _Session()
    monkeypatch.setenv("DOCUBOT_INGEST_URL", "https://docubot.test/jobs")
    monkeypatch.setattr(gateway, "Session", lambda: session)

    gateway.ingest_document_to_docubot(
        source="telegram",
        action="document-upload",
        metadata={"platform": "telegram"},
        local_path=str(upload),
    )

    assert session.post_kwargs["json"]["source_system"] == "telegram"
    assert "source" not in session.post_kwargs["json"]


def _load_drive_watch_module(monkeypatch):
    fake_google_api = types.ModuleType("google_api")
    fake_google_api.build_service = lambda *_args: object()
    monkeypatch.setitem(sys.modules, "google_api", fake_google_api)

    for package_name in ("plugins", "plugins.platforms", "plugins.platforms.telegram"):
        package = types.ModuleType(package_name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, package_name, package)
    monkeypatch.setitem(
        sys.modules,
        "plugins.platforms.telegram.docubot_mcp_gateway",
        gateway,
    )

    module_path = Path(__file__).parents[1] / "scripts" / "hermes_drive_watch.py"
    module_name = "hermes_drive_watch_contract_test"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_drive_watch_requests_multipart_document_review(monkeypatch, tmp_path):
    drive_watch = _load_drive_watch_module(monkeypatch)
    downloaded = tmp_path / "from-drive.pdf"
    downloaded.write_bytes(b"downloaded")
    captured = []
    saved_state = {}

    monkeypatch.setattr(drive_watch, "STATE_DIR", tmp_path / "cron")
    monkeypatch.setattr(drive_watch, "_resolve_folder_id", lambda: "folder-1")
    monkeypatch.setattr(drive_watch, "build_service", lambda *_args: object())
    monkeypatch.setattr(
        drive_watch,
        "_load_state",
        lambda: {"last_seen_time": None, "processed_file_ids": [], "folder_id": None},
    )
    monkeypatch.setattr(
        drive_watch,
        "_list_changed_files",
        lambda *_args: [
            {
                "id": "file-1",
                "name": "from-drive.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-08-04T00:00:00Z",
                "webViewLink": "https://drive.test/file-1",
            }
        ],
    )
    monkeypatch.setattr(drive_watch, "_download_file", lambda *_args: downloaded)
    monkeypatch.setattr(drive_watch, "_save_state", lambda state: saved_state.update(state))
    monkeypatch.setattr(drive_watch, "DRY_RUN", False)
    monkeypatch.setattr(
        drive_watch,
        "ingest_document_to_docubot",
        lambda **kwargs: captured.append(kwargs) or {"status": "accepted"},
    )

    assert drive_watch.main() == 0

    assert len(captured) == 1
    assert captured[0]["action"] == "document_review"
    assert captured[0]["multipart"] is True
    assert captured[0]["source"] == "drive-watch"
    assert captured[0]["stable_key"] == "file-1"
    assert captured[0]["local_path"] == str(downloaded)
    assert captured[0]["metadata"] == {
        "platform": "google-drive",
        "folder_id": "folder-1",
        "file_id": "file-1",
        "file_name": "from-drive.pdf",
        "mime_type": "application/pdf",
        "web_view_link": "https://drive.test/file-1",
    }
    assert saved_state["processed_file_ids"] == ["file-1"]
    assert saved_state["last_seen_time"] == "2026-08-04T00:00:00Z"


def test_drive_watch_dry_run_does_not_save_state(monkeypatch, tmp_path):
    drive_watch = _load_drive_watch_module(monkeypatch)
    downloaded = tmp_path / "from-drive.pdf"
    downloaded.write_bytes(b"downloaded")
    save_calls = []

    monkeypatch.setattr(drive_watch, "STATE_DIR", tmp_path / "cron")
    monkeypatch.setattr(drive_watch, "_resolve_folder_id", lambda: "folder-1")
    monkeypatch.setattr(drive_watch, "build_service", lambda *_args: object())
    monkeypatch.setattr(
        drive_watch,
        "_load_state",
        lambda: {
            "last_seen_time": "2026-08-03T00:00:00Z",
            "processed_file_ids": [],
            "folder_id": "folder-1",
        },
    )
    monkeypatch.setattr(
        drive_watch,
        "_list_changed_files",
        lambda *_args: [
            {
                "id": "file-1",
                "name": "from-drive.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-08-04T00:00:00Z",
                "webViewLink": "https://drive.test/file-1",
            }
        ],
    )
    monkeypatch.setattr(drive_watch, "_download_file", lambda *_args: downloaded)
    monkeypatch.setattr(drive_watch, "_save_state", lambda state: save_calls.append(state))
    monkeypatch.setattr(drive_watch, "DRY_RUN", True)
    monkeypatch.setattr(
        drive_watch,
        "ingest_document_to_docubot",
        lambda **_kwargs: pytest.fail("dry-run must not ingest files"),
    )

    assert drive_watch.main() == 0
    assert save_calls == []
