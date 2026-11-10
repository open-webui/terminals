"""Image pull policy shared by every backend.

Values follow Kubernetes: ``Always``, ``IfNotPresent``, and ``Never``. When
``TERMINALS_IMAGE_PULL_POLICY`` is unset, a moving tag (``:latest`` or no tag)
is pulled on every start and anything else is pulled only when missing, so a
host never stays on a stale ``latest`` image.
"""

import asyncio
import logging
import time

# Settings are imported inside functions: config.py validates with normalize_policy at import time.

log = logging.getLogger(__name__)

POLICIES = ("Always", "IfNotPresent", "Never")

_last_pulled: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


def normalize_policy(value: str) -> str:
    """Return the canonical policy name, or '' for unset. Raises on unknown values."""
    value = (value or "").strip()
    if not value:
        return ""
    for policy in POLICIES:
        if value.lower() == policy.lower():
            return policy
    raise ValueError(f"Image pull policy must be one of {', '.join(POLICIES)}")


def split_image(image: str) -> tuple[str, str]:
    """Split an image reference into repository and tag or digest ('' when absent)."""
    if "@" in image:
        repository, _, digest = image.partition("@")
        return repository, digest
    name = image.rsplit("/", 1)[-1]
    if ":" in name:
        repository, _, tag = image.rpartition(":")
        return repository, tag
    return image, ""


def pull_policy(image: str) -> str:
    """Pull policy for an image: the configured one, else the Kubernetes default."""
    from terminals.config import settings

    configured = normalize_policy(settings.image_pull_policy)
    if configured:
        return configured
    if "@" in image:
        return "IfNotPresent"
    _, tag = split_image(image)
    return "Always" if tag in ("", "latest") else "IfNotPresent"


async def ensure_docker_image(docker, image: str) -> None:
    """Apply the pull policy before a Docker container is created from ``image``.

    ``Always`` re-pulls at most once per ``TERMINALS_IMAGE_PULL_INTERVAL_SECONDS``
    per image, and falls back to the local copy when the registry is unreachable.
    ``IfNotPresent`` leaves pulling to container creation, which fetches missing
    images. ``Never`` requires the image to exist locally.
    """
    from aiodocker.exceptions import DockerError

    from terminals.backends.base import ProvisioningError
    from terminals.config import settings

    policy = pull_policy(image)
    if policy == "IfNotPresent":
        return
    if policy == "Never":
        try:
            await docker.images.inspect(image)
        except DockerError as exc:
            if exc.status == 404:
                raise ProvisioningError(
                    f"Image {image!r} is not present and the pull policy is Never"
                ) from exc
            raise
        return

    lock = _locks.setdefault(image, asyncio.Lock())
    async with lock:
        last = _last_pulled.get(image)
        if last is not None and time.monotonic() - last < settings.image_pull_interval_seconds:
            return
        repository, tag = split_image(image)
        try:
            # Without a tag Docker would pull every tag of the repository.
            await docker.images.pull(repository, tag=tag or "latest")
            log.info("Pulled image %s", image)
        except DockerError as exc:
            try:
                await docker.images.inspect(image)
            except DockerError:
                raise ProvisioningError(f"Could not pull image {image!r}: {exc.message}") from exc
            log.warning("Could not pull image %s, using the local copy: %s", image, exc.message)
        _last_pulled[image] = time.monotonic()
