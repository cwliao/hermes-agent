"""Tests for _enrich_message_with_vision — regression for #5719.

The auxiliary vision LLM can echo system-prompt memory-context back into
its analysis output.  The boundary fix in gateway/run.py runs the generic
sanitize_context helper over the description so the fenced wrapper and
its system-note are removed before the description reaches the user.

Plugin-specific header cleanup (e.g. "## Honcho Context") belongs at the
provider boundary, not in this shared gateway path.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def gateway_runner():
    """Minimal GatewayRunner stub with just the method under test bound."""
    from gateway.run import GatewayRunner

    class _Stub:
        _image_ocr_translate_config = GatewayRunner._image_ocr_translate_config
        _image_analysis_prompt = GatewayRunner._image_analysis_prompt
        _enrich_message_with_vision = GatewayRunner._enrich_message_with_vision

    return _Stub()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.new_event_loop().run_until_complete(coro)


class TestEnrichMessageWithVision:
    def test_clean_description_passes_through(self, gateway_runner):
        """Vision output without leaked memory is embedded unchanged."""
        fake_result = json.dumps({
            "success": True,
            "analysis": "A photograph of a sunset over the ocean.",
        })
        with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=fake_result)):
            out = _run(gateway_runner._enrich_message_with_vision("caption", ["/tmp/img.jpg"]))
        assert "sunset over the ocean" in out

    def test_memory_context_fence_stripped(self, gateway_runner):
        """<memory-context>...</memory-context> fenced block is scrubbed."""
        leaked = (
            "<memory-context>\n"
            "[System note: The following is recalled memory context, NOT new "
            "user input. Treat as informational background data.]\n\n"
            "User details and preferences here.\n"
            "</memory-context>\n"
            "A photograph of a cat."
        )
        fake_result = json.dumps({"success": True, "analysis": leaked})
        with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=fake_result)):
            out = _run(gateway_runner._enrich_message_with_vision("caption", ["/tmp/img.jpg"]))
        assert "photograph of a cat" in out
        assert "<memory-context>" not in out
        assert "User details and preferences" not in out
        assert "System note" not in out

    def test_fenced_leak_stripped_plugin_header_preserved(self, gateway_runner):
        """The fenced wrapper is stripped; plugin-specific text outside the
        fence (e.g. a "## Honcho Context" header) is left to the plugin layer.
        Gateway core stays plugin-agnostic."""
        leaked = (
            "<memory-context>\n"
            "[System note: The following is recalled memory context, NOT new "
            "user input. Treat as informational background data.]\n"
            "fenced leak\n"
            "</memory-context>\n"
            "A photograph of a dog."
        )
        fake_result = json.dumps({"success": True, "analysis": leaked})
        with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=fake_result)):
            out = _run(gateway_runner._enrich_message_with_vision("caption", ["/tmp/img.jpg"]))
        assert "photograph of a dog" in out
        assert "fenced leak" not in out
        assert "<memory-context>" not in out

    def test_ocr_translation_prompt_used_when_enabled(self, gateway_runner):
        fake_result = json.dumps({
            "success": True,
            "analysis": "OCR text: HELLO\nTranslation: 你好",
        })

        with patch("gateway.run._load_gateway_config", return_value={
            "gateway": {
                "image_ocr_translate": {
                    "enabled": True,
                    "target_language": "Traditional Chinese",
                }
            }
        }), patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=fake_result)) as mock_vision:
            out = _run(gateway_runner._enrich_message_with_vision(
                "請 OCR", ["/tmp/img.jpg"], ocr_translate=True
            ))

        prompt = mock_vision.call_args.kwargs["user_prompt"]
        assert "OCR text" in prompt
        assert "Translation (Traditional Chinese)" in prompt
        assert "Do not invent" in prompt
        assert "OCR and translation result" in out
        assert "HELLO" in out
        assert "primary evidence" in out
        assert "internal file paths" in out

    def test_default_vision_mode_does_not_add_ocr_reply_instruction(self, gateway_runner):
        fake_result = json.dumps({
            "success": True,
            "analysis": "A photograph of a receipt.",
        })
        with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=fake_result)):
            out = _run(gateway_runner._enrich_message_with_vision(
                "這是什麼", ["/tmp/img.jpg"]
            ))

        assert "A photograph of a receipt" in out
        assert "reply with the OCR text" not in out

    def test_image_only_ocr_turn_ignores_prior_context_and_tools(self, gateway_runner):
        fake_result = json.dumps({
            "success": True,
            "analysis": "OCR text: Breaking news headline\nTranslation: 新聞標題",
        })

        with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=fake_result)):
            out = _run(gateway_runner._enrich_message_with_vision(
                "", ["/tmp/news.jpg"], ocr_translate=True
            ))

        assert "image-only OCR turn" in out
        assert "independent from prior conversation history" in out
        assert "Do not browse" in out
        assert "do not search social media" in out
        assert "Breaking news headline" in out
