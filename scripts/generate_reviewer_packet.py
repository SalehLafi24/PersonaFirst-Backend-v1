"""Generate a reviewer-friendly markdown packet for the recommendation rubric.

Strips technical detail (scores, intent_contributions, attribute keys)
and presents per-customer:
  - profile (1 line)
  - recent purchase history (names + simple category)
  - recommendations (names + simple category)
  - the 5 rubric questions

Output: scrollable single markdown file. Pair it with
seed_data/eval/recommendation_rubric_template.csv (reviewer fills in).

Usage:
    python scripts/generate_reviewer_packet.py --workspace mumzworld_v3_sample > reviewer_packet.md
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from app.core.database import SessionLocal
from app.models.customer import CustomerInteraction
from app.models.product import Product
from app.models.workspace import Workspace
from app.services.customer_recommendation_service import recommend_for_customer


_DEFAULT_FIXTURE = ROOT / "seed_data" / "eval" / "recommendation_customers.json"


_BRIEF = """# Recommendation Quality Review

Thanks for taking 30 minutes to do this.

## What you're rating

For each of 10 customers below, you'll see:
- **Profile** — one-line description.
- **Recently bought** — products the customer has actually purchased.
- **Recommended** — the 10 products our system suggests.

You answer 5 questions per customer about whether the recommendations
make sense. Record your answers in
`seed_data/eval/recommendation_rubric_template.csv`.

## What we're trying to learn

The system passes its automated quality checks. We want a fresh human
take on whether the recs *feel* right. **Trust your first instinct.**
There are no right answers — your honest read is exactly what we need.

## The 5 questions, in plain language

**Every question is worded so that `yes` (or `none` for q2) is the good
answer.** You never answer "yes" to report a problem — if something feels
wrong, that's a `no` (or a non-zero count on q2). Trust your first instinct.

| # | Question (yes / none = good) | Answer | ✓ good vs ✗ problem |
|---|---|---|---|
| q1 | **Anchor sense** — do the recs match what this customer is clearly into? | yes / no | ✓ yes = history + recs feel coherent · ✗ no = recs ignore the obvious shopping pattern |
| q2 | **Bizarre items** — how many recs are clearly out of place? (a count) | none / 1 / >1 | ✓ none · ✗ 1 or >1 — e.g. adult skincare to a baby-book buyer is bizarre; a slightly different product type is not |
| q3 | **Complement vs more-of-same** — do the recs *go with* what they bought, rather than just being *more of* the same? | yes / partial / no | ✓ yes = adds a different dimension to their basket · partial = mixed · ✗ no = clones / near-duplicates |
| q4 | **No bad repeats** — are things they just bought kept out of the recs (or clearly justified if shown)? | yes / no | ✓ yes = no awkward repeats · ✗ no = something they bought yesterday is being re-pitched |
| q5 | **Surprise** — is there at least one interesting expansion — something they probably didn't expect but still makes sense? | yes / no | ✓ yes = at least one defensible "huh, that's a nice idea" · ✗ no = everything is predictable |

## How to record your answers

In `recommendation_rubric_template.csv`, for each customer row, fill:
- `reviewer` — your name or initials
- `reviewed_at` — today's date
- `q1_anchor_sense` — yes / no _(yes = good)_
- `q2_no_bizarre_items` — none / 1 / >1 _(none = good)_
- `q3_complement_quality` — yes / partial / no _(yes = good)_
- `q4_saturation_respect` — yes / no _(yes = good)_
- `q5_surprise` — yes / no _(yes = good)_
- `notes` — any specific observation (1-2 sentences)

Save the file as `recommendation_rubric_<your_name>_<date>.csv` so we
can compare reviewers without overwriting.

## Things to ignore

- Don't try to understand the scoring or how the recs were generated.
- Don't compare to anyone else's review (we want your independent take).
- If a question feels ambiguous, answer based on your most natural reading
  and put a note explaining why it was hard to call.
- If you don't know what a product is, that's OK — judge by the name.

## Time

~3 minutes per customer × 10 customers = ~30 minutes total.

---
"""


def _attr_label(attrs: dict[str, str | None]) -> str:
    """Build a compact, human-friendly category label."""
    parts = []
    pt = attrs.get("product_type")
    if pt:
        parts.append(pt.replace("_", " "))
    age = attrs.get("age_group")
    if age:
        parts.append(age)
    use = attrs.get("use_case")
    if use:
        parts.append(f"for {use}")
    return ", ".join(parts) if parts else "(no category)"


def _print_history(db, workspace_id: int, customer_id: str, max_items: int = 8):
    rows = (
        db.query(CustomerInteraction.product_id, CustomerInteraction.occurred_at)
        .filter(
            CustomerInteraction.workspace_id == workspace_id,
            CustomerInteraction.customer_id == customer_id,
        )
        .order_by(CustomerInteraction.occurred_at.desc())
        .limit(max_items)
        .all()
    )
    if not rows:
        print("- _(no recent purchases)_")
        return
    pids = [r[0] for r in rows]
    products_by_pid = {
        p.product_id: p
        for p in db.query(Product).filter(
            Product.workspace_id == workspace_id,
            Product.product_id.in_(pids),
        ).all()
    }
    # Need attributes for category labels.
    from app.models.product import ProductAttribute
    attr_rows = (
        db.query(ProductAttribute.product_id,
                 ProductAttribute.attribute_id,
                 ProductAttribute.attribute_value)
        .filter(ProductAttribute.product_id.in_(
            [p.id for p in products_by_pid.values()]
        ))
        .all()
    )
    attrs_by_dbid: dict[int, dict[str, str]] = {}
    for db_id, attr, val in attr_rows:
        attrs_by_dbid.setdefault(db_id, {})[attr] = val

    for pid, occurred in rows:
        prod = products_by_pid.get(pid)
        if prod is None:
            continue
        nm = (prod.name or pid)[:75]
        attrs = attrs_by_dbid.get(prod.id, {})
        cat = _attr_label(attrs)
        print(f"- **{nm}** &nbsp; _{cat}_")


def _print_recs(response):
    for i, rec in enumerate(response.recommendations, start=1):
        nm = (rec.name or rec.product_id)[:75]
        cat = _attr_label(rec.attributes or {})
        print(f"{i}. **{nm}** &nbsp; _{cat}_")


_QUESTION_BLOCK = """
**Your ratings for this customer** _(record in CSV — `yes` / `none` = good)_:
- q1 anchor sense: yes / no  _(yes = good)_
- q2 bizarre items: none / 1 / >1  _(none = good)_
- q3 complement quality: yes / partial / no  _(yes = good)_
- q4 no bad repeats: yes / no  _(yes = good)_
- q5 surprise: yes / no  _(yes = good)_
- notes:

---
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--fixture", type=Path, default=_DEFAULT_FIXTURE)
    parser.add_argument("--top-n", type=int, default=10)
    args = parser.parse_args()

    if not args.fixture.exists():
        raise SystemExit(f"fixture not found: {args.fixture}")
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    customers = fixture.get("customers") or []

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")

        print(_BRIEF)
        for i, customer in enumerate(customers, start=1):
            cid = customer["customer_id"]
            print(f"## Customer {i} of {len(customers)}: `{cid}`")
            print()
            print(f"_Profile: {customer.get('profile_label')}_")
            print()
            print("### Recently bought")
            _print_history(db, ws.id, cid)
            print()
            print(f"### Recommended (top {args.top_n})")
            response = recommend_for_customer(
                db, workspace_id=ws.id, customer_id=cid, top_n=args.top_n,
            )
            _print_recs(response)
            print(_QUESTION_BLOCK)
        print()
        print(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
              f"for workspace `{args.workspace}` (id={ws.id})._")
    finally:
        db.close()


if __name__ == "__main__":
    main()
