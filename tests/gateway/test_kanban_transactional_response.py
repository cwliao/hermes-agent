"""Regression tests for the Kanban final-response delivery boundary."""

from gateway.run import _is_kanban_transactional_turn


PROMPT = (
    "四條 lane (native_hermes / claude / grok / agy) 各自獨立產出一句秋天諧音梗。"
    "Verifier 驗證；Synthesizer 整理。"
)


def test_four_lane_request_disables_early_text_streaming():
    assert _is_kanban_transactional_turn(PROMPT)
    assert _is_kanban_transactional_turn({"text": PROMPT})


def test_ordinary_turn_keeps_normal_streaming_path():
    assert not _is_kanban_transactional_turn("請幫我整理這段文字")
    assert not _is_kanban_transactional_turn(
        "請說明 Kanban swarm 的概念，不要建立任何任務"
    )


def test_explicit_kanban_create_request_also_waits_for_receipt():
    assert _is_kanban_transactional_turn("請建立一個 Kanban task 給 default")
