from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("workspace_id", "product_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    group_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    repurchase_behavior: Mapped[str | None] = mapped_column(String(50), nullable=True)
    repurchase_window_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommendation_role: Mapped[str] = mapped_column(
        String(50), nullable=False, default="same_use_case", server_default="same_use_case"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP")
    )


class ProductAttribute(Base):
    __tablename__ = "product_attributes"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    attribute_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attribute_value: Mapped[str] = mapped_column(String(255), nullable=False)
