from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Valid statuses: suggested, approved, rejected, archived


class AttributeValueRelationship(Base):
    __tablename__ = "attribute_value_relationships"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    # DB columns are attribute_id_1 / attribute_value_1 / attribute_id_2 / attribute_value_2.
    # The first positional arg to mapped_column() sets the actual column name; the Python
    # attribute name used everywhere in queries stays source_attribute_id etc.
    source_attribute_id: Mapped[str] = mapped_column("attribute_id_1", String(255), nullable=False)
    source_value: Mapped[str] = mapped_column("attribute_value_1", String(255), nullable=False)
    target_attribute_id: Mapped[str] = mapped_column("attribute_id_2", String(255), nullable=False)
    target_value: Mapped[str] = mapped_column("attribute_value_2", String(255), nullable=False)
    # relationship_type and source do not exist as DB columns. Kept as plain Python
    # class attributes (not Mapped, not mapped_column) so that existing code which
    # passes them to the constructor does not raise TypeError. They are never written
    # to or read from the database.
    relationship_type = None
    source = None
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    lift: Mapped[float] = mapped_column(Float, nullable=False)
    pair_count: Mapped[int] = mapped_column(Integer, nullable=False)
    strength: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="suggested")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
