"""Structural guard for Telegram's single DocuBot ingestion call site."""

import ast
from pathlib import Path


def test_telegram_adapter_has_one_docubot_ingestion_call_site():
    adapter_path = (
        Path(__file__).parents[1]
        / "plugins"
        / "platforms"
        / "telegram"
        / "adapter.py"
    )
    tree = ast.parse(adapter_path.read_text(encoding="utf-8"))
    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ingest_document_to_docubot"
    ]

    assert len(call_sites) == 1
