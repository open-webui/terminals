"""Shared parsing utilities for K8s-style resource strings."""

import re
from decimal import Decimal

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti)?$")
_CPU_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(m)?$")

_SIZE_MULT = {"": 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}

_QUANTITY_RE = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([KMGTPE]i|[numkMGTPE]|[eE][+-]?\d+)?")
_QUANTITY_MULT = {
    **_SIZE_MULT, "Pi": 1024**5, "Ei": 1024**6,
    "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3"),
    "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18,
}


def parse_kubernetes_quantity(value: str) -> Decimal:
    """Compare PVC requests in bytes while preserving their native units."""
    match = _QUANTITY_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"Invalid Kubernetes quantity: {value!r}")
    number, suffix = match.groups(default="")
    if suffix in _QUANTITY_MULT:
        return Decimal(number) * _QUANTITY_MULT[suffix]
    return Decimal(number + suffix)


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
