"""One-off script to apply the pilot labels.

Not intended to be reused -- this is the simulated labeler's pass against
the 12 pilot products. In production, a human would use a labeling UI
(or edit the YAML directly). Kept under scripts/ so the labeling pass
is auditable from the repo.

Run once:
    python scripts/_run_pilot_labels.py
"""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "seed_data" / "eval" / "attribute_gold_sample.json"

LABELED_BY = "claude_pilot_v1"

# Each entry: labels, label_time_seconds (synthetic; recorded relative to
# cognitive load), friction_notes capturing real ambiguities and
# system disagreements that surfaced while applying the guide.
PILOT_LABELS = {
    "TC-MCPL2470": {
        # Disney Mickey Cotton Bibs 2pcs - Blue
        "labels": {"product_type": "bib", "age_group": "infant", "gender": "male", "use_case": "feeding"},
        "label_time_seconds": 35,
        "friction_notes": [
            "Disney/Mickey character licensing + 'Blue' colour together suggest gender=male",
            "guide says 'most infant care items default to unisex unless explicitly coded otherwise' -- character themes ARE coding but the guide doesn't say so explicitly",
            "agreed with system on all four values, but the gender rule that justified it isn't in the guide",
        ],
    },
    "HR-9788172451097": {
        # BabyVision Reusable Diaper - Red Bug Printed
        "labels": {"product_type": "diaper", "age_group": "infant", "gender": "unisex", "use_case": "diapering"},
        "label_time_seconds": 30,
        "friction_notes": [
            "system says gender=female; 'Red Bug Printed' is gender-neutral (bugs aren't female-coded, red isn't strongly female-coded)",
            "DISAGREEMENT: system gender=female -> labeled unisex",
            "pattern flag: possible system bias toward gender=female on bug/animal/red prints",
        ],
    },
    "PCG-4466": {
        # Dantoy My Little Princess Breakfast Set - 32pcs
        "labels": {"product_type": "playset", "age_group": "toddler", "gender": "female", "use_case": "play"},
        "label_time_seconds": 90,
        "friction_notes": [
            "'My Little Princess' theme is strongly female-coded; system says unisex",
            "DISAGREEMENT: system gender=unisex -> labeled female",
            "use_case ambiguity: this is a play SET that mimics breakfast/feeding; does pretend feeding count as use_case=feeding, or play?",
            "guide gap: themed playsets (princess breakfast, doctor kit, school) need an explicit rule for use_case",
            "labeled use_case=play because the product is held by a child as a toy, not used in real feeding",
        ],
    },
    "SGT-A2443": {
        # Playskool Mr. Potato Head Tater Tub Set
        "labels": {"product_type": "playset", "age_group": "kids", "gender": "unisex", "use_case": "play"},
        "label_time_seconds": 30,
        "friction_notes": [
            "'Set' in name -- is this a bundle? Reading edge_cases: 'bundles spanning multiple moments' -- this is a single themed set (not multi-component), so the bundle rule shouldn't trigger",
            "guide could clarify: when does 'Set' mean a bundle vs a single themed product",
            "all four values agreed with system",
        ],
    },
    "ROE-BA15072NA": {
        # Baby Aspen "Tricerasocks" Plush Gift Set with Socks for Baby
        "labels": {"product_type": "socks", "age_group": "infant", "gender": "unisex", "use_case": None},
        "label_time_seconds": 75,
        "friction_notes": [
            "TRIPLE STRESS: socks (apparel) + plush (toy) + 'Gift Set' (bundle)",
            "guide says apparel without context defaults to use_case=null",
            "system labeled use_case=play -- presumably from the plush component",
            "DISAGREEMENT: system use_case=play -> labeled null",
            "guide gap: apparel-bundled-with-plush -- does the plush dominate use_case, or does the apparel rule win? Strict reading: apparel is the dominant product (the actual item bought is socks); plush is themed packaging",
            "product_type=socks correct under apparel rule (most specific value)",
        ],
    },
    "WCME-PMBTW-Config": {
        # Twinkle Hands Personalised Men Black T-Shirt - White Print
        "labels": {"product_type": "shirt", "age_group": "adult", "gender": "male", "use_case": None},
        "label_time_seconds": 40,
        "friction_notes": [
            "'Personalised Men' clearly adult-targeted (not teen)",
            "system says age_group=teen; labeled adult",
            "DISAGREEMENT: system age_group=teen -> labeled adult",
            "pattern flag: system seems to use 'teen' as a catch-all for >8 years instead of 'adult' for adult-targeted products",
        ],
    },
    "SBFZE-1001": {
        # Zephyr Metal Mechanix NX0
        "labels": {"product_type": "construction_toy", "age_group": "teen", "gender": "unisex", "use_case": "learning"},
        "label_time_seconds": 80,
        "friction_notes": [
            "metal construction toys historically male-marketed; modern catalogs lean unisex",
            "DISAGREEMENT: system gender=male -> labeled unisex",
            "use_case ambiguity: guide says 'when in doubt prefer play unless plainly educational; default to learning if age-graded'",
            "Metal Mechanix is age-graded engineering -- fits the age-graded rule for learning",
            "DISAGREEMENT: system use_case=null -> labeled learning",
            "borderline rule application: a stricter reading of 'when in doubt prefer play' would default play. The age-graded clause tipped me to learning.",
        ],
    },
    "EM-4455": {
        # Playmobil Bunnies School
        "labels": {"product_type": "playset", "age_group": "kids", "gender": "unisex", "use_case": "play"},
        "label_time_seconds": 95,
        "friction_notes": [
            "Playmobil is figurine-and-scenery system, not Lego-style construction",
            "DISAGREEMENT: system product_type=construction_toy -> labeled playset",
            "bunnies + school -- neither strongly gendered; system says female",
            "DISAGREEMENT: system gender=female -> labeled unisex",
            "name contains 'School' but this is a TOY about school, not an actual school-day item",
            "guide says use_case=school is for 'used during the school day' (backpacks, lunch boxes, stationery) -- not for school-themed toys",
            "DISAGREEMENT: system use_case=null -> labeled play",
            "guide gap: themed toys (school, hospital, kitchen) -- do they take their themed use_case, or play? Guide does not address explicitly.",
        ],
    },
    "TAS-725": {
        # b.box Cutlery Set - Bubblegum
        "labels": {"product_type": None, "age_group": "infant", "gender": "unisex", "use_case": "feeding"},
        "label_time_seconds": 50,
        "friction_notes": [
            "CUTLERY isn't in any canonical example or active AAV; guide says do NOT invent values during labeling, mark null and flag",
            "product_type=null per open-vocab rule; flagging cutlery as a likely missing AAV",
            "system has no product_type AND use_case=null; the cutlery context is plainly feeding",
            "DISAGREEMENT: system use_case=null -> labeled feeding",
            "MANIFEST GAP: 'cutlery' is a clear missing product_type for baby-care catalog (b.box is a major brand of baby cutlery)",
        ],
    },
    "HSM-111761": {
        # Casabella Sink Sider Faucet Sponge Holder
        "labels": {"product_type": None, "age_group": "adult", "gender": "unisex", "use_case": None},
        "label_time_seconds": 55,
        "friction_notes": [
            "kitchen organization product -- does this even belong in mumzworld's baby-care catalog?",
            "product_type=null (no fitting category for kitchen org)",
            "system gender=female on a gender-neutral sponge holder",
            "DISAGREEMENT: system gender=female -> labeled unisex",
            "system age_group=null on an adult-targeted product",
            "DISAGREEMENT: system age_group=null -> labeled adult",
            "CATALOG SCOPE ISSUE: kitchen organization seems out-of-domain; flag for review",
        ],
    },
    "NP-131869": {
        # Natural Factors Super Multi Iron Free 90 Tablets
        "labels": {"product_type": "supplement", "age_group": "adult", "gender": "unisex", "use_case": None},
        "label_time_seconds": 30,
        "friction_notes": [
            "multivitamin tablets, clearly adult-targeted (not a pediatric chewable form)",
            "system says age_group=null; labeled adult",
            "DISAGREEMENT: system age_group=null -> labeled adult",
            "pattern flag: system under-applies age_group=adult on adult supplements",
        ],
    },
    "NP-104862": {
        # Nordic Naturals Probiotic Gummies For Kids
        "labels": {"product_type": "supplement", "age_group": "kids", "gender": "unisex", "use_case": None},
        "label_time_seconds": 30,
        "friction_notes": [
            "name explicitly says 'For Kids' -- kids age band is correct, not teen",
            "DISAGREEMENT: system age_group=teen -> labeled kids",
            "pattern flag: system mis-extracted age_group; possibly regex matched a different token in the name",
        ],
    },
}


def main() -> None:
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    products_by_id = {p["product_id"]: p for p in data["products"]}

    labeled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    n_disagreements = 0
    n_disagree_products = 0
    n_friction_notes = 0
    total_time = 0
    by_category_time: dict[str, int] = {}
    by_category_count: dict[str, int] = {}

    for pid, vals in PILOT_LABELS.items():
        p = products_by_id.get(pid)
        if p is None:
            raise SystemExit(f"product not found in sample: {pid}")
        if not p.get("is_pilot"):
            raise SystemExit(
                f"{pid} is not flagged is_pilot=True; "
                f"refusing to label a non-pilot product"
            )
        p["labels"] = vals["labels"]
        p["labeled_by"] = LABELED_BY
        p["labeled_at"] = labeled_at
        p["label_time_seconds"] = vals["label_time_seconds"]
        p["friction_notes"] = list(vals["friction_notes"])

        # Stats.
        total_time += vals["label_time_seconds"]
        n_friction_notes += len(vals["friction_notes"])
        cat = p.get("pilot_stress_category") or "?"
        by_category_time[cat] = by_category_time.get(cat, 0) + vals["label_time_seconds"]
        by_category_count[cat] = by_category_count.get(cat, 0) + 1

        cur = p.get("current_system_values") or {}
        product_disagreed = False
        for k, v in vals["labels"].items():
            if cur.get(k) != v:
                n_disagreements += 1
                product_disagreed = True
        if product_disagreed:
            n_disagree_products += 1

    SAMPLE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Labeled {len(PILOT_LABELS)} pilot products as {LABELED_BY!r}")
    print(f"Total label time     : {total_time}s  ({total_time / 60:.1f} min)")
    print(f"Mean per product     : {total_time / len(PILOT_LABELS):.1f}s")
    print(f"Friction notes total : {n_friction_notes}")
    print(f"Mean per product     : {n_friction_notes / len(PILOT_LABELS):.1f}")
    print()
    print(f"Products with >=1 disagreement vs system : "
          f"{n_disagree_products} / {len(PILOT_LABELS)}  "
          f"({n_disagree_products / len(PILOT_LABELS) * 100:.0f}%)")
    print(f"Total per-attribute disagreements        : "
          f"{n_disagreements}  ({n_disagreements / (len(PILOT_LABELS) * 4) * 100:.0f}% of slots)")
    print()
    print("Mean time by stress category:")
    for cat in sorted(by_category_time):
        n = by_category_count[cat]
        avg = by_category_time[cat] / n
        print(f"  {cat:<22}  n={n}  total={by_category_time[cat]}s  mean={avg:.0f}s")


if __name__ == "__main__":
    main()
