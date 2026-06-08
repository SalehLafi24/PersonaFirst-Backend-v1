"""V1 product-to-product recommendation route.

GET /api/recommendations/products/{product_id}
    workspace_id  : int (required)
    top_n         : int (optional, default 5)
    customer_id   : str (optional, accepted but unused in V1)

The route is intentionally thin: it parses query params, calls
`recommendation_v1_service.recommend`, and returns the structured
response. All logic, telemetry, and tier handling live in the service.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.recommendation_v1_service import recommend

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])


@router.get("/products/{product_id}")
def recommend_for_product(
    product_id: str,
    workspace_id: int = Query(..., description="Workspace id."),
    top_n: int = Query(5, ge=1, le=50,
                       description="Maximum number of recommendations."),
    customer_id: str | None = Query(
        None, description="Reserved for future personalization; "
                          "accepted but unused in V1."),
    db: Session = Depends(get_db),
):
    response = recommend(
        db=db, workspace_id=workspace_id,
        anchor_product_id=product_id,
        top_n=top_n, customer_id=customer_id,
    )
    return asdict(response)
