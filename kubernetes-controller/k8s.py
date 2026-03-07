"""Kubernetes helpers for managing Terminal CRDs.

Handles CRUD operations on ``terminals.open-webui.com/v1alpha1`` custom resources
and reading the API key from the associated Secret.
"""

import asyncio
import base64
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

import kubernetes
from kubernetes import client as k8s

from terminals.config import (
    DEFAULT_CPU_LIMIT,
    DEFAULT_CPU_REQUEST,
    DEFAULT_IDLE_TIMEOUT_MINUTES,
    DEFAULT_IMAGE,
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_MEMORY_REQUEST,
    DEFAULT_PERSISTENCE_ENABLED,
    DEFAULT_PERSISTENCE_SIZE,
    DEFAULT_STORAGE_CLASS,
    TERMINAL_NAMESPACE,
)

log = logging.getLogger(__name__)

API_GROUP = "open-webui.com"
API_VERSION = "v1alpha1"
RESOURCE_PLURAL = "terminals"


def _terminal_name(user_id: str) -> str:
    """Deterministic Terminal CR name from a user ID."""
    short = hashlib.sha256(user_id.encode()).hexdigest()[:12]
    return f"terminal-{short}"


def _load_kube_config():
    """Load in-cluster config, falling back to kubeconfig for local dev."""
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()


# Ensure config is loaded at import time.
_load_kube_config()


def get_terminal(user_id: str) -> Optional[dict]:
    """Get the Terminal CR for a user, or None if it doesn't exist."""
    custom_api = k8s.CustomObjectsApi()
    name = _terminal_name(user_id)
    try:
        return custom_api.get_namespaced_custom_object(
            group=API_GROUP,
            version=API_VERSION,
            namespace=TERMINAL_NAMESPACE,
            plural=RESOURCE_PLURAL,
            name=name,
        )
    except k8s.exceptions.ApiException as e:
        if e.status == 404:
            return None
        raise


def _list_running_terminals() -> list[dict]:
    """List all Terminal CRs in Running phase."""
    custom_api = k8s.CustomObjectsApi()
    try:
        result = custom_api.list_namespaced_custom_object(
            group=API_GROUP,
            version=API_VERSION,
            namespace=TERMINAL_NAMESPACE,
            plural=RESOURCE_PLURAL,
        )
        return [
            t for t in result.get("items", [])
            if (t.get("status") or {}).get("phase") == "Running"
        ]
    except k8s.exceptions.ApiException:
        return []


def create_terminal(user_id: str) -> dict:
    """Create a Terminal CR for a user and return it."""
    custom_api = k8s.CustomObjectsApi()
    name = _terminal_name(user_id)

    body = {
        "apiVersion": f"{API_GROUP}/{API_VERSION}",
        "kind": "Terminal",
        "metadata": {
            "name": name,
            "namespace": TERMINAL_NAMESPACE,
            "labels": {
                "app.kubernetes.io/managed-by": "terminals-orchestrator",
                "open-webui.com/user-id": user_id,
            },
        },
        "spec": {
            "userId": user_id,
            "image": DEFAULT_IMAGE,
            "resources": {
                "requests": {
                    "cpu": DEFAULT_CPU_REQUEST,
                    "memory": DEFAULT_MEMORY_REQUEST,
                },
                "limits": {
                    "cpu": DEFAULT_CPU_LIMIT,
                    "memory": DEFAULT_MEMORY_LIMIT,
                },
            },
            "idleTimeoutMinutes": DEFAULT_IDLE_TIMEOUT_MINUTES,
            "packages": [],
            "pipPackages": [],
            "persistence": {
                "enabled": DEFAULT_PERSISTENCE_ENABLED,
                "size": DEFAULT_PERSISTENCE_SIZE,
                "storageClass": DEFAULT_STORAGE_CLASS,
            },
        },
    }

    try:
        return custom_api.create_namespaced_custom_object(
            group=API_GROUP,
            version=API_VERSION,
            namespace=TERMINAL_NAMESPACE,
            plural=RESOURCE_PLURAL,
            body=body,
        )
    except k8s.exceptions.ApiException as e:
        if e.status == 409:
            # Already exists — return existing
            return get_terminal(user_id)  # type: ignore[return-value]
        raise


def delete_terminal(user_id: str) -> bool:
    """Delete the Terminal CR for a user. Returns True if deleted, False if not found."""
    custom_api = k8s.CustomObjectsApi()
    name = _terminal_name(user_id)
    try:
        custom_api.delete_namespaced_custom_object(
            group=API_GROUP,
            version=API_VERSION,
            namespace=TERMINAL_NAMESPACE,
            plural=RESOURCE_PLURAL,
            name=name,
        )
        return True
    except k8s.exceptions.ApiException as e:
        if e.status == 404:
            return False
        raise


def touch_activity(user_id: str) -> None:
    """Update lastActivityAt on the Terminal CR to prevent idle culling."""
    custom_api = k8s.CustomObjectsApi()
    name = _terminal_name(user_id)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        custom_api.patch_namespaced_custom_object_status(
            group=API_GROUP,
            version=API_VERSION,
            namespace=TERMINAL_NAMESPACE,
            plural=RESOURCE_PLURAL,
            name=name,
            body={"status": {"lastActivityAt": now}},
        )
    except k8s.exceptions.ApiException as e:
        if e.status != 404:
            log.warning("Failed to touch activity for %s: %s", name, e)


def get_api_key(secret_name: str) -> Optional[str]:
    """Read the API key from a Kubernetes Secret."""
    core_v1 = k8s.CoreV1Api()
    try:
        secret = core_v1.read_namespaced_secret(secret_name, TERMINAL_NAMESPACE)
        raw = secret.data.get("api-key", "")
        return base64.b64decode(raw).decode() if raw else None
    except k8s.exceptions.ApiException as e:
        if e.status == 404:
            return None
        raise


def ensure_terminal(user_id: str) -> dict:
    """Get or create a Terminal CR. If the terminal was idled, re-create the pod.

    Returns the Terminal CR dict (may still be in Pending/Provisioning phase).
    """
    terminal = get_terminal(user_id)
    if terminal is None:
        return create_terminal(user_id)

    phase = (terminal.get("status") or {}).get("phase")
    if phase == "Idle":
        # Terminal was idled — delete and re-create to spawn a fresh pod
        delete_terminal(user_id)
        return create_terminal(user_id)

    return terminal


async def wait_for_ready(user_id: str, timeout: float) -> Optional[dict]:
    """Poll until the Terminal CR reaches Running phase or timeout expires.

    Returns the Terminal CR dict if ready, or None on timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        terminal = get_terminal(user_id)
        if terminal:
            phase = (terminal.get("status") or {}).get("phase")
            if phase == "Running":
                return terminal
        await asyncio.sleep(1.0)
    return None
