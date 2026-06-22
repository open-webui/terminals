"""Policy database model."""

from sqlalchemy import Column, DateTime, String, UniqueConstraint, func
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


class PolicyLifecycle(Base):
    __tablename__ = "policy_lifecycles"
    __table_args__ = (
        UniqueConstraint("policy_id", name="uq_policy_lifecycles_policy"),
    )

    id = Column(String, primary_key=True)
    policy_id = Column(String, nullable=False, index=True)
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<PolicyLifecycle policy_id={self.policy_id!r}>"


class PolicyLifecycleState(Base):
    __tablename__ = "policy_lifecycle_states"
    __table_args__ = (
        UniqueConstraint("user_id", "policy_id", name="uq_policy_lifecycle_states_user_policy"),
    )

    id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    policy_id = Column(String, nullable=False, index=True)
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return (
            f"<PolicyLifecycleState user_id={self.user_id!r} "
            f"policy_id={self.policy_id!r}>"
        )
