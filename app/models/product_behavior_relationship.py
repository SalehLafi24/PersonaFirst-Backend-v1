from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ProductBehaviorRelationship(Base):
    __tablename__ = "product_behavior_relationships"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "source_product_db_id", "target_product_db_id",
            name="uq_behavior_rel",
        ),
        Index("ix_behavior_rel_ws_source", "workspace_id", "source_product_db_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    source_product_db_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    target_product_db_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    strength: Mapped[float] = mapped_column(Float, nullable=False)
    customer_overlap_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_customer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
