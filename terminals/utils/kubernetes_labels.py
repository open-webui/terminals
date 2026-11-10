"""Kubernetes label formatting shared by the backends and operator."""

import re


def template_label(template_id: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", template_id.lower()).strip("-")[:20]
