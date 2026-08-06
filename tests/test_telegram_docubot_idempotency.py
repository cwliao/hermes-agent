from plugins.platforms.telegram.docubot_mcp_gateway import (
    build_idempotency_key,
    build_telegram_document_stable_key,
)


def test_telegram_document_key_is_scoped_by_chat_and_message():
    first = build_telegram_document_stable_key(-1001, 42, "same-file")
    other_chat = build_telegram_document_stable_key(-1002, 42, "same-file")
    other_message = build_telegram_document_stable_key(-1001, 43, "same-file")

    assert first != other_chat
    assert first != other_message


def test_telegram_document_retry_reuses_same_idempotency_key():
    stable = build_telegram_document_stable_key(-1001, 42, "same-file")

    assert build_idempotency_key(
        source="telegram", action="document_review", stable_key=stable
    ) == build_idempotency_key(
        source="telegram", action="document_review", stable_key=stable
    )
