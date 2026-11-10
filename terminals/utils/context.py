"""Stable identities for template environments. This encoding is a storage contract."""
import hashlib
import json
from fastapi import HTTPException

DEFAULT_CONTEXT_ID = "default"
TEMPLATE_HEADER = "X-Terminal-Template-Id"
CHAT_HEADER = "X-Terminal-Chat-Id"
MODEL_HEADER = "X-Terminal-Model-Id"
CONTEXT_HEADER = "X-Terminal-Context-Id"
CONTEXT_ID_ANNOTATION = "openwebui.com/context-id"
CONTEXT_TYPE_LABEL = "openwebui.com/context-type"
CONTEXT_HASH_LABEL = "openwebui.com/context-hash"


def validate_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("Identifier must contain 1–2048 characters")
    if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid identifier")
    return value


def normalize_context_id(value: str | None) -> str:
    value = value or DEFAULT_CONTEXT_ID
    if value == DEFAULT_CONTEXT_ID:
        return value
    prefix, separator, identifier = value.partition(":")
    if not separator or prefix not in {"user", "chat", "model"}:
        raise ValueError("Invalid terminal context")
    validate_identifier(identifier)
    return value


def context_hash(context_id: str) -> str:
    return hashlib.sha256(context_id.encode()).hexdigest()[:12]


def get_terminal_id(template_id: str, context_id: str = DEFAULT_CONTEXT_ID) -> str:
    """Return the stable instance/storage ID for an already resolved template context."""
    validate_identifier(template_id)
    normalize_context_id(context_id)
    encoded = json.dumps([template_id, context_id], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()[:32]


def terminal_identity(template_id, scope, *, user_id, chat_id=None, model_id=None):
    if scope is None:
        context_id = DEFAULT_CONTEXT_ID
    else:
        if scope not in {"user", "chat", "model"}:
            raise HTTPException(422, "Invalid template scope")
        value = {"user": user_id, "chat": chat_id, "model": model_id}[scope]
        if not value:
            raise HTTPException(409, f"This terminal requires a {scope}")
        try:
            context_id = f"{scope}:{validate_identifier(value)}"
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return context_id, get_terminal_id(template_id, context_id)
