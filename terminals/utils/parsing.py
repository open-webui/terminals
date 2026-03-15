"""Shared parsing utilities for K8s-style resource strings."""

import re

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti)?$")
_CPU_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(m)?$")

_SIZE_MULT = {"": 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def parse_size(value: str) -> int:
    """Parse K8s-style size string to bytes. '512Mi' → 536_870_912."""
    m = _SIZE_RE.match(str(value).strip())
    if not m:
        return int(value)
    num, suffix = float(m.group(1)), m.group(2) or ""
    return int(num * _SIZE_MULT[suffix])


def parse_memory(value: str) -> int:
    """Alias for ``parse_size`` — parses memory values to bytes."""
    return parse_size(value)


def parse_cpu_nanos(value: str) -> int:
    """Parse K8s CPU string to nanocpus. '2' → 2_000_000_000, '500m' → 500_000_000."""
    m = _CPU_RE.match(str(value).strip())
    if not m:
        return int(float(value) * 1_000_000_000)
    num, suffix = float(m.group(1)), m.group(2) or ""
    if suffix == "m":
        return int(num * 1_000_000)
    return int(num * 1_000_000_000)
