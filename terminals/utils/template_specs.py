"""Shared helpers for resolved terminal template specs."""

from terminals.config import settings
from terminals.db.session import async_session
from terminals.utils.context import validate_identifier
from terminals.utils.parsing import parse_cpu_nanos, parse_size


class TemplateNotFoundError(Exception):
    """Raised when a template does not exist."""


def merge_template_defaults(template_data: dict) -> dict:
    """Merge env var defaults with template overrides."""
    defaults = {}
    image = settings.image
    if settings.backend in ("kubernetes", "kubernetes-operator"):
        image = settings.effective_kubernetes_image
    if image:
        defaults["image"] = image
    result = {**defaults, **{k: v for k, v in template_data.items() if v is not None}}
    # Enforce current caps at provisioning time, including for older templates.
    for field, cap, parse in (
        ("cpu_limit", settings.max_cpu, parse_cpu_nanos),
        ("memory_limit", settings.max_memory, parse_size),
    ):
        if cap:
            ceiling = parse(cap)
            if ceiling <= 0:
                raise ValueError(f"{field} cap must be positive")
            if not result.get(field) or not 0 < parse(result[field]) <= ceiling:
                result[field] = cap
    return result


async def resolve_template_spec(template_id: str) -> dict:
    """Look up a template by ID and merge its configuration with server defaults."""
    validate_identifier(template_id)
    if async_session is None:
        raise RuntimeError("A database is required to resolve templates")

    from sqlalchemy import select

    from terminals.models.template import Template

    async with async_session() as session:
        row = await session.execute(select(Template).where(Template.id == template_id))
        template = row.scalar_one_or_none()
        if template is None:
            raise TemplateNotFoundError(template_id)

        return merge_template_defaults(template.data or {})
