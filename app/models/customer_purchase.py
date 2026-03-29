from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CustomerPurchase(Base):
    __tablename__ = "customer_purchases"
    __table_args__ = (
        Index("ix_purchases_ws_cust", "workspace_id", "customer_id"),
        Index("ix_purchases_ws_cust_prod", "workspace_id", "customer_id", "product_db_id"),
        Index("ix_purchases_ws_cust_group", "workspace_id", "customer_id", "group_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    product_db_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)  # denormalized external id
    group_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), server_default=text("CURRENT_TIMESTAMP")
    )
