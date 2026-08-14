ASSISTANT_INVALID_RESPONSE_CODES = frozenset(
    {"empty_response", "incomplete_turn", "invalid_response", "invalid_structured_output"}
)
ASSISTANT_FAILURE_CODES = frozenset(
    {
        *ASSISTANT_INVALID_RESPONSE_CODES,
        "command_unavailable",
        "missing_image",
        "no_response",
        "provider_error",
        "timeout",
        "tool_use_rejected",
        "transport_error",
        "unsupported_image",
    }
)
ASSISTANT_RETRYABLE_FAILURE_CODES = frozenset(
    {
        "empty_response",
        "incomplete_turn",
        "invalid_structured_output",
        "provider_error",
        "timeout",
        "tool_use_rejected",
    }
)
