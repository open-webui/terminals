"""Template database model."""

from sqlalchemy import Column, DateTime, String, ForeignKey, UniqueConstraint, func
from sqlalchemy.types import JSON

from terminals.models.base import Base


class Template(Base):
    __tablename__ = "templates"

    id = Column(String, primary_key=True)
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<Template id={self.id!r}>"


class TemplateLifecycle(Base):
    __tablename__ = "template_lifecycles"
    __table_args__ = (
        UniqueConstraint("template_id", name="uq_template_lifecycles_template"),
    )

    id = Column(String, primary_key=True)
    template_id = Column(String, ForeignKey("templates.id"), nullable=False, index=True)
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<TemplateLifecycle template_id={self.template_id!r}>"


class TemplateLifecycleState(Base):
    __tablename__ = "template_lifecycle_states"
    __table_args__ = (
        UniqueConstraint(
            "template_id",
            "context_id",
            name="uq_template_lifecycle_context",
        ),
    )

    id = Column(String, primary_key=True)
    template_id = Column(String, ForeignKey("templates.id"), nullable=False, index=True)
    context_id = Column(String, nullable=False, default="default", index=True)
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return (
            f"<TemplateLifecycleState "
            f"template_id={self.template_id!r} context_id={self.context_id!r}>"
        )
