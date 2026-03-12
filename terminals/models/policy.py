"""Policy database model."""

from sqlalchemy import Column, DateTime, String, func
from sqlalchemy.types import JSON

from terminals.models.base import Base


class Policy(Base):
    __tablename__ = "policies"

    id = Column(String, primary_key=True)
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<Policy id={self.id!r}>"
