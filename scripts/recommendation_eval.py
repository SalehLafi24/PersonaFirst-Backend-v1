"""CLI: recommendation engine conformance + constraint eval.

Runs the rec engine against a fixture of synthetic customers and reports
a per-customer x per-check matrix.

Usage:
    python scripts/recommendation_eval.py --workspace mumzworld_v3_sample
    python scripts/recommendation_eval.py --workspace mumzworld_v3_sample --top-n 10
    python scripts/recommendation_eval.py --workspace mumzworld_v3_sample --customer synthetic_baby_essentials

Reads:
  - seed_data/eval/recommendation_customers.json (customer fixture)
  - seed_data/attribute_manifest.json (manifest weights)
  - workspace DB (rec engine inputs + customer interactions)

Writes: nothing. Pure read.

Exit code:
  0 if all customers pass all error-severity checks.
  1 if any error-severity check fails.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from app.core.database import SessionLocal
from app.models.workspace import Workspace
from app.services.attribute_engine import load_manifest
from app.services.customer_recommendation_service import recommend_for_customer
from app.services.eval.recommendation_eval import (
    DIVERSITY_CHECKS,
    HARD_CONSTRAINT_CHECKS,
    WEIGHT_CONFORMANCE_CHECKS,
    check_diversity_floor,
    check_hard_constraints,
    check_weight_conformance,
)


_DEFAULT_FIXTURE = ROOT / "seed_data" / "eval" / "recommendation_customers.json"


# Order checks come out in the matrix; matches discovery order in the
# eval module so the CLI is stable.
ALL_CHECK_NAMES = (
    *HARD_CONSTRAINT_CHECKS,
    *WEIGHT_CONFORMANCE_CHECKS,
    *DIVERSITY_CHECKS,
)


def _status_glyph(passed: bool, has_warns: bool) -> str:
    if not passed:
        return "FAIL"
    if has_warns:
        return "warn"
    return "ok  "


def _run_for_customer(
    db, *, workspace_id: int, customer_id: str, top_n: int,
    manifest_weights: dict[str, float],
):
    """Run rec engine + all checkers for one customer. Returns dict of
    check_name -> CheckResult."""
    response = recommend_for_customer(
        db, workspace_id=workspace_id, customer_id=customer_id,
        top_n=top_n,
    )
    persona_confidence = float(getattr(response.persona, "confidence_overall", 0.0) or 0.0)

    out = {}
    out.update(check_hard_constraints(
        db, response, workspace_id=workspace_id,
        customer_id=customer_id, requested_top_n=top_n,
    ))
    out.update(check_weight_conformance(
        response, customer_id=customer_id,
        manifest_weights=manifest_weights,
        persona_confidence=persona_confidence,
    ))
    out.update(check_diversity_floor(
        response, customer_id=customer_id,
    ))
    return response, out


def _print_matrix(rows: list[tuple[str, dict]], check_names: tuple[str, ...]):
    """Print per-customer x per-check matrix."""
    # Determine column widths.
    cust_w = max(len(cid) for cid, _ in rows) if rows else 8
    cust_w = max(cust_w, len("customer"))
    print()
    print(f"{'customer':<{cust_w}}  | " + " | ".join(
        f"{n:>5}" for n in [c[:5] for c in check_names]
    ))
    print("-" * cust_w + "--+-" + "-+-".join("-" * 5 for _ in check_names) + "-")
    for cid, results in rows:
        cells = []
        for name in check_names:
            r = results.get(name)
            if r is None:
                cells.append(" -   ")
                continue
            has_warns = any(v.severity == "warn" for v in r.violations)
            cells.append(f"{_status_glyph(r.passed, has_warns):>5}")
        print(f"{cid:<{cust_w}}  | " + " | ".join(cells))


def _print_violations(rows):
    """Print all violations encountered, grouped by customer + check."""
    any_violation = False
    for cid, results in rows:
        cust_violations = [(n, r) for n, r in results.items() if r.violations]
        if not cust_violations:
            continue
        any_violation = True
        print()
        print(f"--- {cid} ---")
        for name, r in cust_violations:
            for v in r.violations:
                tag = "FAIL" if v.severity == "error" else "warn"
                print(f"  [{tag}] {name}: {v.message}")
    if not any_violation:
        print()
        print("No violations.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recommendation engine conformance + constraint eval. "
                    "Read-only.",
    )
    parser.add_argument("--workspace", required=True,
                        help="workspace slug (e.g. mumzworld_v3_sample)")
    parser.add_argument("--fixture", type=Path, default=_DEFAULT_FIXTURE,
                        help=f"customer fixture JSON (default: {_DEFAULT_FIXTURE})")
    parser.add_argument("--top-n", type=int, default=10,
                        help="top-N recs to request per customer (default: 10)")
    parser.add_argument("--customer", default=None,
                        help="run only this customer_id (debug aid)")
    parser.add_argument("--verbose", action="store_true",
                        help="print full violation messages, not just matrix")
    args = parser.parse_args()

    if not args.fixture.exists():
        raise SystemExit(f"fixture not found: {args.fixture}")
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    customers = fixture.get("customers") or []
    if args.customer:
        customers = [c for c in customers if c["customer_id"] == args.customer]
        if not customers:
            raise SystemExit(f"customer {args.customer!r} not in fixture")

    manifest = load_manifest()
    manifest_weights = manifest.score_weights()

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == args.workspace).first()
        if ws is None:
            raise SystemExit(f"workspace not found: {args.workspace!r}")

        rows: list[tuple[str, dict]] = []
        for c in customers:
            cid = c["customer_id"]
            try:
                _resp, results = _run_for_customer(
                    db, workspace_id=ws.id, customer_id=cid,
                    top_n=args.top_n, manifest_weights=manifest_weights,
                )
                rows.append((cid, results))
            except Exception as e:
                print(f"  {cid}: ERROR running rec engine: {e!r}")
                rows.append((cid, {}))

        # Header.
        print(f"workspace            : {args.workspace} (id={ws.id})")
        print(f"customers evaluated  : {len(rows)}")
        print(f"top_n                : {args.top_n}")
        print(f"manifest weights     : {manifest_weights}")

        _print_matrix(rows, ALL_CHECK_NAMES)
        _print_violations(rows)

        # Aggregate pass-rate per check.
        print()
        print("aggregate pass-rate per check (errors only):")
        for name in ALL_CHECK_NAMES:
            results_for_check = [r.get(name) for _, r in rows if r.get(name)]
            if not results_for_check:
                print(f"  {name:<40}  -")
                continue
            n_pass = sum(1 for r in results_for_check if r.passed)
            n = len(results_for_check)
            print(f"  {name:<40}  {n_pass}/{n}  "
                  f"({n_pass/n*100:.0f}%)")

        # Exit code: 1 if any error-level failure across any customer/check.
        any_error = any(
            not r.passed
            for _, results in rows
            for r in results.values()
        )
        sys.exit(1 if any_error else 0)
    finally:
        db.close()


if __name__ == "__main__":
    main()
