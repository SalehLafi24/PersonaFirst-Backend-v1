from sqlalchemy.orm import Session

from app.schemas.signal_strength import (
    AudienceSignalRead,
    SignalDistributionRead,
)
from app.services.signal_strength_service import (
    _compute_from_stats,
    _workspace_affinity_stats,
    _workspace_behavioral_stats,
    _workspace_purchase_stats,
)


def compute_audience_signal(
    db: Session,
    workspace_id: int,
    customer_ids: list[str],
) -> AudienceSignalRead:
    """Aggregate signal strength across a list of customer_ids.

    Duplicates are removed before computation.  Unknown customer_ids
    are included with signal_strength = 0.0.
    """
    # Deduplicate while preserving first-seen order
    unique_ids = list(dict.fromkeys(customer_ids))

    # Compute workspace-wide stats once
    purchase_stats = _workspace_purchase_stats(db, workspace_id)
    affinity_stats = _workspace_affinity_stats(db, workspace_id)
    behavioral_stats = _workspace_behavioral_stats(db, workspace_id)

    # Per-customer signal strength (unknown customers get 0.0 naturally)
    strengths = [
        _compute_from_stats(cid, purchase_stats, affinity_stats, behavioral_stats).customer_signal_strength
        for cid in unique_ids
    ]

    audience_size = len(strengths)
    avg = round(sum(strengths) / audience_size, 6)

    return AudienceSignalRead(
        audience_size=audience_size,
        average_signal_strength=avg,
        min_signal_strength=min(strengths),
        max_signal_strength=max(strengths),
        signal_distribution=SignalDistributionRead(
            low=sum(1 for s in strengths if s < 0.4),
            medium=sum(1 for s in strengths if 0.4 <= s <= 0.7),
            high=sum(1 for s in strengths if s > 0.7),
        ),
    )
