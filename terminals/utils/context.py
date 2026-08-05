"""Terminal context helpers."""

import hashlib

DEFAULT_CONTEXT_ID = "default"
CONTEXT_HEADER = "X-Terminal-Context-Id"
CONTEXT_ID_ANNOTATION = "openwebui.com/context-id"
CONTEXT_TYPE_LABEL = "openwebui.com/context-type"
CONTEXT_HASH_LABEL = "openwebui.com/context-hash"


def normalize_context_id(value: str | None) -> str:
    context_id = (value or "").strip()
    if not context_id:
        return DEFAULT_CONTEXT_ID
    if len(context_id) > 256:
        raise ValueError("context_id is too long")
    if any(ord(char) < 32 for char in context_id):
        raise ValueError("context_id contains control characters")
    if context_id != DEFAULT_CONTEXT_ID and not context_id.startswith(
        ("chat:", "automation:")
    ):
        raise ValueError("only chat and automation contexts are supported")
    return context_id


def context_hash(context_id: str) -> str:
    return hashlib.sha256(context_id.encode()).hexdigest()[:12]
