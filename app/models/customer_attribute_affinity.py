from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CustomerAttributeAffinity(Base):
    __tablename__ = "customer_attribute_affinities"
    __table_args__ = (
        UniqueConstraint("workspace_id", "customer_id", "attribute_id", "attribute_value",
                         name="uq_customer_affinity"),
        Index("ix_affinities_ws_cust", "workspace_id", "customer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attribute_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attribute_value: Mapped[str] = mapped_column(String(255), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
