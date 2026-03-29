from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Valid statuses: suggested, approved, rejected, archived


class AttributeValueRelationship(Base):
    __tablename__ = "attribute_value_relationships"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    source_attribute_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_value: Mapped[str] = mapped_column(String(255), nullable=False)
    target_attribute_id: Mapped[str] = mapped_column(String(255), nullable=False)
    target_value: Mapped[str] = mapped_column(String(255), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(100), nullable=False, default="complementary")
    source: Mapped[str] = mapped_column(String(100), nullable=False, default="cooccurrence")
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    lift: Mapped[float] = mapped_column(Float, nullable=False)
    pair_count: Mapped[int] = mapped_column(Integer, nullable=False)
    strength: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="suggested")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
