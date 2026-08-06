from penhin.agent.token_accounting import estimate_context_tokens, estimate_text_tokens


def test_multilingual_estimate_does_not_treat_cjk_like_ascii() -> None:
    assert estimate_text_tokens("a" * 100) == 25
    assert estimate_text_tokens("中" * 100) == 100


def test_context_estimate_uses_latest_provider_usage_plus_trailing_messages() -> None:
    messages = [
        {"role": "user", "content": "old" * 1000},
        {"role": "assistant", "content": "answer", "_meta": {"usage": {"context_tokens": 700}}},
        {"role": "user", "content": "中" * 10},
    ]

    estimate = estimate_context_tokens(messages)

    assert estimate.usage_tokens == 700
    assert estimate.trailing_tokens > 10
    assert estimate.tokens == estimate.usage_tokens + estimate.trailing_tokens
    assert estimate.last_usage_index == 1


def test_context_estimate_ignores_invalid_usage() -> None:
    estimate = estimate_context_tokens(
        [{"role": "assistant", "content": "hello", "_meta": {"usage": {"context_tokens": 0}}}]
    )

    assert estimate.source == "message_estimate"
    assert estimate.tokens > 0


def test_context_estimate_discounts_messages_snipped_after_provider_usage() -> None:
    messages = [
        {"role": "user", "content": "x" * 400, "_meta": {"snipped": True}},
        {"role": "assistant", "content": "answer", "_meta": {"usage": {"context_tokens": 700}}},
    ]

    estimate = estimate_context_tokens(messages)

    assert estimate.usage_tokens < 700
    assert estimate.tokens == estimate.usage_tokens
