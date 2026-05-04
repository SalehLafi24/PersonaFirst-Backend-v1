"""Read-only snapshot of /api/merge_suggestions output for v3.

No DB writes. Used to compare BEFORE/AFTER state when changing the
detector logic. Saves to a JSON file under /tmp so two runs can be
diffed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models.workspace import Workspace

WS_SLUG = "mumzworld_v3_sample"
ATTR = "product_type"
OUT_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("merge_snapshot.json")


def main() -> None:
    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.slug == WS_SLUG).first()
        if ws is None:
            raise SystemExit(f"workspace {WS_SLUG} not found")
        ws_id = ws.id
    finally:
        db.close()

    client = TestClient(app)
    r = client.get("/admin/taxonomy/api/merge_suggestions",
                   params={"workspace_id": ws_id, "attribute": ATTR})
    if r.status_code != 200:
        raise SystemExit(f"GET failed: {r.status_code} {r.text[:200]}")
    body = r.json()

    OUT_PATH.write_text(json.dumps(body, indent=2))
    print(f"Wrote snapshot to {OUT_PATH}")
    print(f"total            : {body.get('total')}")
    print(f"by_type_count    : {body.get('by_type_count')}")
    print(f"hierarchy_candidates: {len(body.get('hierarchy_candidates', []))}")
    items = body.get("items", [])

    print()
    print("== Top parent_child by combined_count (recommended direction) ==")
    pc = [s for s in items if s["merge_type"] == "parent_child"][:25]
    for s in pc:
        alt = s.get("alternative_direction", {})
        print(f"  {s['source_cluster']:<22} -> {s['target_cluster']:<22} "
              f"dir_conf={s.get('direction_confidence','?'):<6} "
              f"score={s.get('direction_score',0):+.2f} "
              f"alt={alt.get('source','?')}->{alt.get('target','?')} "
              f"({alt.get('score',0):+.2f})")

    print()
    print("== All normalization_variant ==")
    for s in items:
        if s["merge_type"] == "normalization_variant":
            print(f"  {s['source_cluster']:<28} -> {s['target_cluster']:<28}  "
                  f"executable={s['executable']}")

    print()
    print("== Top semantic_duplicate ==")
    sd = [s for s in items if s["merge_type"] == "semantic_duplicate"][:10]
    for s in sd:
        print(f"  {s['source_cluster']:<28} -> {s['target_cluster']:<28}  "
              f"conf={s['confidence']}")


if __name__ == "__main__":
    main()
