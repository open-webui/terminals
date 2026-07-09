"""Kubernetes pod scheduling settings."""

import json
from typing import Any

from terminals.config import settings


def _parse_node_selector(raw: str) -> dict[str, str] | None:
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("{"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("TERMINALS_KUBERNETES_NODE_SELECTOR must be an object")
        return {str(key): str(value) for key, value in data.items()}

    selector = {}
    for pair in raw.split(","):
        if "=" not in pair:
            raise ValueError(
                "TERMINALS_KUBERNETES_NODE_SELECTOR must be JSON or k=v pairs"
            )
        key, value = pair.split("=", 1)
        selector[key.strip()] = value.strip()
    return selector or None


def _parse_tolerations(raw: str) -> list[dict[str, Any]] | None:
    raw = raw.strip()
    if not raw:
        return None
    data = json.loads(raw)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError("TERMINALS_KUBERNETES_TOLERATIONS must be a JSON array")
    return data


def node_selector() -> dict[str, str] | None:
    return _parse_node_selector(settings.kubernetes_node_selector)


def tolerations() -> list[dict[str, Any]] | None:
    return _parse_tolerations(settings.kubernetes_tolerations)
