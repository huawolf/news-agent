"""LLM protocol detection and endpoint normalization."""

import re
from urllib.parse import urlsplit, urlunsplit


OPENAI_CHAT = "openai_chat"
OPENAI_RESPONSES = "openai_responses"
ANTHROPIC_MESSAGES = "anthropic_messages"
LLM_PROTOCOLS = (OPENAI_CHAT, OPENAI_RESPONSES, ANTHROPIC_MESSAGES)

_PROTOCOL_SUFFIXES = {
    OPENAI_CHAT: ("/chat/completions",),
    OPENAI_RESPONSES: ("/responses", "/response"),
    ANTHROPIC_MESSAGES: ("/messages",),
}
_CANONICAL_SUFFIX = {
    OPENAI_CHAT: "/chat/completions",
    OPENAI_RESPONSES: "/responses",
    ANTHROPIC_MESSAGES: "/messages",
}


def detect_endpoint_protocol(endpoint: str) -> str | None:
    """Detect a protocol from a complete endpoint path, if present."""
    path = urlsplit(str(endpoint).strip()).path.lower().rstrip("/")
    for protocol, suffixes in _PROTOCOL_SUFFIXES.items():
        if any(path.endswith(suffix) for suffix in suffixes):
            return protocol
    return None


def detect_model_protocol(model: str) -> str | None:
    """Detect a protocol from common OpenAI and Anthropic model names."""
    value = str(model).strip().lower()
    if any(name in value for name in ("claude", "opus", "sonnet", "haiku")):
        return ANTHROPIC_MESSAGES
    if re.search(r"(^|[/_.:-])(gpt|chatgpt|o1|o3|o4)(?:[-_.:]|$)", value):
        return OPENAI_RESPONSES
    return None


def infer_llm_protocol(endpoint: str, model: str) -> str:
    """Infer protocol from endpoint first, then model, with chat compatibility fallback."""
    return (
        detect_endpoint_protocol(endpoint)
        or detect_model_protocol(model)
        or OPENAI_CHAT
    )


def resolve_llm_endpoint(endpoint: str, protocol: str) -> str:
    """Return the final request URL for a base URL or complete endpoint."""
    if protocol not in LLM_PROTOCOLS:
        raise ValueError(f"unsupported LLM protocol: {protocol}")

    parsed = urlsplit(str(endpoint).strip())
    path = parsed.path.rstrip("/")
    lower_path = path.lower()
    matching_suffixes = _PROTOCOL_SUFFIXES[protocol]
    if any(lower_path.endswith(suffix) for suffix in matching_suffixes):
        final_path = path
    else:
        final_path = path
        for suffixes in _PROTOCOL_SUFFIXES.values():
            matched = next(
                (suffix for suffix in suffixes if lower_path.endswith(suffix)), None
            )
            if matched:
                final_path = path[: -len(matched)].rstrip("/")
                break
        final_path = f"{final_path}{_CANONICAL_SUFFIX[protocol]}"

    return urlunsplit(
        (parsed.scheme, parsed.netloc, final_path, parsed.query, parsed.fragment)
    )
