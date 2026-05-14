"""Customer + CustomerInteraction tables.

Customer
    one row per (workspace_id, customer_id). External customer_id is a
    free-form string supplied by the importing system.

CustomerInteraction
    append-only log of (customer, product, interaction_type, occurred_at).
    Persona extraction reads from this table only.

interaction_type is open-vocabulary so future signals (view, add_to_cart,
wishlist) slot in without schema change. Today only 'purchase' is emitted.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


# Recognised interaction types. Free-form in the column, but listed here
# as constants so callers don't typo-drift. The persona service weights
# them via a config table (TODO future) — today only 'purchase' contributes.
INTERACTION_PURCHASE = "purchase"
INTERACTION_VIEW = "view"
INTERACTION_ADD_TO_CART = "add_to_cart"
INTERACTION_WISHLIST = "wishlist"


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "customer_id",
                         name="uq_customer_ws_cid"),
        Index("ix_customer_ws_cid", "workspace_id", "customer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False,
    )
    customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )


class CustomerInteraction(Base):
    __tablename__ = "customer_interactions"
    __table_args__ = (
        Index("ix_cinter_ws_cid_occurred",
              "workspace_id", "customer_id", "occurred_at"),
        Index("ix_cinter_ws_pid", "workspace_id", "product_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id"), nullable=False,
    )
    customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    interaction_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default=INTERACTION_PURCHASE,
        server_default=INTERACTION_PURCHASE,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
