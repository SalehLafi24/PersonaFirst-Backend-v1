"""Bridge-batch labeling pass against Guide v1.1.

Mirrors scripts/_run_pilot_labels.py. Applies labels to 30 products
selected deterministically via hash(product_id, 'bridge') across layers
1, 2, 3a, 3b, and 3_5 of the gold sample.

Goal: stress-test Guide v1.1 at scale before committing the full ~75
remaining. Decision gate: if friction-note count per product trends
sharply higher than the pilot (4-7 / product), stop and revise to v1.2.

Run once:
    python scripts/_run_bridge_labels.py
"""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "seed_data" / "eval" / "attribute_gold_sample.json"

LABELED_BY = "claude_bridge_v1"
GUIDE_VERSION = "1.1"


# Each entry mirrors the pilot script's schema: labels +
# label_time_seconds + friction_notes. Friction notes record only the
# guide gaps the bridge labeler hit -- not every disagreement with
# system. Aim is to surface NEW rules v1.2 might need.
BRIDGE_LABELS = {
    "IB-6063482": {
        # Kinetic Sand - Slice N' Surprise
        "labels": {"product_type": "sensory_toy", "age_group": "kids", "gender": "unisex", "use_case": "play"},
        "label_time_seconds": 35,
        "friction_notes": [],
    },
    "TOP-LB283EVA-WT": {
        # Lovely Baby - BMW Motorbike - White
        "labels": {"product_type": "ride_on", "age_group": "toddler", "gender": "unisex", "use_case": "play"},
        "label_time_seconds": 50,
        "friction_notes": [
            "ride_on default age band not explicit in guide; product name has no age cue. Defaulted to toddler (typical 2-5y range), midpoint of toddler/kids. v1.2 candidate: ride_on default age rule.",
        ],
    },
    "FGCO-13276": {
        # Star Wars - Mandalorian Puzzle - 200pcs
        "labels": {"product_type": "puzzle", "age_group": "teen", "gender": "male", "use_case": "learning"},
        "label_time_seconds": 55,
        "friction_notes": [],
    },
    "MHMI-NB1047": {
        # D'Addario - Acoustic Guitar String Set
        "labels": {"product_type": None, "age_group": "adult", "gender": "unisex", "use_case": None},
        "label_time_seconds": 70,
        "friction_notes": [
            "OUT-OF-DOMAIN: real guitar accessories. v1.1 catalog-scope rule applies: product_type=null, flag. age_group=adult per adult-utility rule.",
        ],
    },
    "TOP-MD6203": {
        # Mideer - Kids Backpack - Very Hungry Caterpillar
        "labels": {"product_type": "backpack", "age_group": "kids", "gender": "unisex", "use_case": "school"},
        "label_time_seconds": 40,
        "friction_notes": [],
    },
    "AALC-ABBO20007": {
        # Aden + Anais - Muslin Burpy Bib - Map The Stars
        "labels": {"product_type": "bib", "age_group": "infant", "gender": "unisex", "use_case": "feeding"},
        "label_time_seconds": 30,
        "friction_notes": [],
    },
    "NG-6251001309126": {
        # Fine - Female Adult Diaper Pants
        "labels": {"product_type": "feminine_care", "age_group": "adult", "gender": "female", "use_case": "diapering"},
        "label_time_seconds": 60,
        "friction_notes": [
            "Adult incontinence in baby-care catalog: product_type ambiguous between 'diaper' (child diaper AAV) and 'feminine_care' (adult feminine product). Picked feminine_care to match catalog intent. v1.2 candidate: explicit rule for adult incontinence -> feminine_care.",
        ],
    },
    "BTF-10103SRG-L": {
        # Surf Gecko T-Shirt - White
        "labels": {"product_type": "shirt", "age_group": "kids", "gender": "unisex", "use_case": None},
        "label_time_seconds": 60,
        "friction_notes": [
            "Apparel without explicit age cue: guide v1.1 says 'apparel sized in age ranges -> dominant band' but says nothing when no age range is given. Defaulted to kids based on theme (Gecko = playful) but this is a guess. v1.2 candidate: apparel age_group default rule.",
        ],
    },
    "NES-12460556": {
        # Nescafe Cappuccino Latte Coffee
        "labels": {"product_type": "coffee", "age_group": "adult", "gender": "unisex", "use_case": None},
        "label_time_seconds": 35,
        "friction_notes": [],
    },
    "SBF-LS_IFA_GDBS": {
        # Little Story - Galaxy Dreams Baby Swing
        "labels": {"product_type": "bouncer", "age_group": "infant", "gender": "unisex", "use_case": None},
        "label_time_seconds": 45,
        "friction_notes": [
            "Bouncers/swings don't fit any use_case value cleanly (not feeding/diapering/bathing/sleeping/etc). Soothing isn't an AAV value. Per OOV: leave null. v1.2 candidate: soothing use_case OR explicit 'no use_case for bouncers' rule.",
        ],
    },
    "AL-GWY55": {
        # Mattel - JW Dominion Velociraptor Figure
        "labels": {"product_type": "action_figure", "age_group": "kids", "gender": "unisex", "use_case": "play"},
        "label_time_seconds": 35,
        "friction_notes": [],
    },
    "FAH-8809581487949": {
        # Missha - M Perfect Cover BB Cream
        "labels": {"product_type": "makeup", "age_group": "adult", "gender": "unisex", "use_case": None},
        "label_time_seconds": 90,
        "friction_notes": [
            "BB Cream / beauty: v1.1 rejects historical female-coding for products without explicit gender language. But beauty product CATEGORIES (BB cream, makeup) are overwhelmingly female-marketed. Strict v1.1 reading -> unisex; retail intuition -> female. v1.2 candidate: female-coded product categories (makeup, hair clips) without explicit phrase still default unisex, OR add an exception list.",
        ],
    },
    "YT-84632-CONFIG": {
        # Mad Toys - Mad Scientist Costumes
        "labels": {"product_type": "costume", "age_group": "kids", "gender": "unisex", "use_case": "party"},
        "label_time_seconds": 45,
        "friction_notes": [
            "Guide explicitly lists costumes under use_case=party. But costumes are also used for daily dress-up play, not just parties. Picked party per guide. v1.2 candidate: 'costume + theme' might split to play vs party (e.g., 'mad scientist costume' is everyday dress-up; 'birthday costume' is party).",
        ],
    },
    "30926520-SN071": {
        # Essen - Personalized Bento Lunch Box - Purple Mermaid
        "labels": {"product_type": "lunch_box", "age_group": "kids", "gender": "female", "use_case": "school"},
        "label_time_seconds": 40,
        "friction_notes": [
            "'Mermaid' is strongly female-coded (similar to princess/fairy) but NOT in v1.1's explicit female-coded character list. v1.2 candidate: extend female-coded list to include mermaid.",
        ],
    },
    "KOJ-912289": {
        # Toy School - Catch Ball Game
        "labels": {"product_type": "game", "age_group": "kids", "gender": "unisex", "use_case": "play"},
        "label_time_seconds": 30,
        "friction_notes": [],
    },
    "MTC-998-P": {
        # Megastar - Ride On Mini Space Motorcycle - Pink
        "labels": {"product_type": "ride_on", "age_group": "toddler", "gender": "unisex", "use_case": "play"},
        "label_time_seconds": 75,
        "friction_notes": [
            "Pink color in baby/toy retail is asymmetrically female-coded vs blue. v1.1 says 'colour alone is NOT gender coding' (example: blue diaper -> unisex). Strict reading -> unisex. But pink alone in toys often DOES signal female targeting. Picked unisex per literal v1.1. v1.2 CANDIDATE (HIGH PRIORITY): explicit rule for pink in toys/apparel -- is the symmetric color rule the right call?",
        ],
    },
    "NGT-FCIPT115445": {
        # Faber-Castell - A4 Drawing Book
        "labels": {"product_type": "notebook", "age_group": "teen", "gender": "unisex", "use_case": "learning"},
        "label_time_seconds": 40,
        "friction_notes": [
            "Faber-Castell is artist-grade brand; drawing book serves both kids and adults. No age cue in name. Defaulted to teen as broad middle. v1.2 candidate: artist-grade tools default age rule.",
        ],
    },
    "TW-55977-BLACK": {
        # Intex - Reef Rider Masks
        "labels": {"product_type": "swim_accessory", "age_group": "teen", "gender": "unisex", "use_case": "swimming"},
        "label_time_seconds": 35,
        "friction_notes": [],
    },
    "II-2751": {
        # Bruder - Construction Truck & Excavator
        "labels": {"product_type": "vehicle_toy", "age_group": "kids", "gender": "unisex", "use_case": "play"},
        "label_time_seconds": 40,
        "friction_notes": [],
    },
    "SGCO-45701": {
        # If - Sticky Highlighter Tabs
        "labels": {"product_type": "stationery", "age_group": "adult", "gender": "unisex", "use_case": "school"},
        "label_time_seconds": 50,
        "friction_notes": [
            "Sticky tabs / highlighter accessories serve teens (high school) AND adults (office). v1.1 'adult-utility' rule + brand context (If is a UK gift/stationery brand) tips to adult. v1.2 candidate: 'study/office stationery without age cue' default to adult.",
        ],
    },
    "NGT-CB2254": {
        # DK Bicycles - Devo 16 Inch
        "labels": {"product_type": "bicycle", "age_group": "kids", "gender": "unisex", "use_case": "play"},
        "label_time_seconds": 50,
        "friction_notes": [
            "Bicycles don't fit use_case cleanly. Travel = transporting the child (strollers, car seats). Play = active recreation (closer fit). Picked play. v1.2 candidate: explicit rule for bicycles/scooters -- play vs travel.",
        ],
    },
    "SV-ROW20390": {
        # DockATot - Deluxe+ Cover - Ginger Shibori
        "labels": {"product_type": "furniture", "age_group": "infant", "gender": "unisex", "use_case": "sleeping"},
        "label_time_seconds": 50,
        "friction_notes": [],
    },
    "31971811-77050": {
        # Casdon - Delonghi Coffee Machine Toy
        "labels": {"product_type": "playset", "age_group": "kids", "gender": "unisex", "use_case": "play"},
        "label_time_seconds": 40,
        "friction_notes": [],
    },
    "PME-H004": {
        # Salt & Crystal - Himalayan Lamp
        "labels": {"product_type": None, "age_group": "adult", "gender": "unisex", "use_case": None},
        "label_time_seconds": 45,
        "friction_notes": [
            "OUT-OF-DOMAIN: Himalayan salt lamp is adult home decor. v1.1 catalog-scope rule applies: product_type=null + flag. CATALOG SCOPE PATTERN: kitchen/decor items keep appearing (3rd in this batch).",
        ],
    },
    "LBG-22.131.007": {
        # Yvonne Ellen - Doggie Hi Ball Glass
        "labels": {"product_type": None, "age_group": "adult", "gender": "unisex", "use_case": None},
        "label_time_seconds": 40,
        "friction_notes": [
            "OUT-OF-DOMAIN: adult dining glass with dog illustration. v1.1 catalog-scope rule applies. System gender=female (likely from designer name 'Yvonne Ellen'); v1.1 -> unisex (animal print, no explicit gender).",
        ],
    },
    "16880979-XLZ00XZ940": {
        # Xcluzive - HS164 Fashion Hair Clips
        "labels": {"product_type": "hair_care", "age_group": "kids", "gender": "female", "use_case": None},
        "label_time_seconds": 60,
        "friction_notes": [
            "'Fashion Hair Clips' -- product CATEGORY is overwhelmingly female-marketed but no explicit gender phrase ('Girls'). Strict v1.1 -> unisex; retail intuition -> female. Same friction as BB Cream (FAH-8809581487949). v1.2 candidate: female-coded product categories list.",
        ],
    },
    "THCL-A22803-PPLBK": {
        # Astro - Men's Analog-Digital Watch
        "labels": {"product_type": None, "age_group": "adult", "gender": "male", "use_case": None},
        "label_time_seconds": 30,
        "friction_notes": [],
    },
    "DSC112": {
        # DumaSafe - Car Mirror Baby Monitor
        "labels": {"product_type": None, "age_group": "infant", "gender": "unisex", "use_case": "travel"},
        "label_time_seconds": 45,
        "friction_notes": [
            "Car safety accessory ('car mirror baby monitor') has no fitting AAV in workspace. Possibly a stroller_accessory analog for car seats. MANIFEST GAP: car_seat_accessory missing from AAVs.",
        ],
    },
    "TC-1867868424700": {
        # Keeeper - Disney 4-In-1 Potty - Mickey Mouse
        "labels": {"product_type": None, "age_group": "toddler", "gender": "unisex", "use_case": "diapering"},
        "label_time_seconds": 50,
        "friction_notes": [
            "Potty (toilet training device) has no AAV. MANIFEST GAP: 'potty' / 'training_potty' missing from AAVs. Mickey Mouse character: per v1.1 unisex (character licensing alone doesn't gender-code).",
        ],
    },
    "HSM-161777": {
        # Addis - Clip & Close 4L Cereal Container
        "labels": {"product_type": None, "age_group": "adult", "gender": "unisex", "use_case": None},
        "label_time_seconds": 35,
        "friction_notes": [
            "OUT-OF-DOMAIN: adult kitchen storage. 4th catalog-scope case in this batch (HSM-111761 from pilot, MHMI-NB1047, PME-H004, LBG-22.131.007). System gender=female on neutral kitchen item -- v1.1 rejects this.",
        ],
    },
}


def main() -> None:
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    products_by_id = {p["product_id"]: p for p in data["products"]}

    labeled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    n_friction = 0
    n_friction_per_product = []
    n_disagreements = 0
    n_disagree_products = 0
    total_time = 0
    by_layer_count: dict[str, int] = {}
    by_layer_friction: dict[str, int] = {}

    for pid, vals in BRIDGE_LABELS.items():
        p = products_by_id.get(pid)
        if p is None:
            raise SystemExit(f"product not found in sample: {pid}")
        if p.get("is_pilot"):
            raise SystemExit(
                f"{pid} is flagged is_pilot=True; bridge batch must NOT "
                f"overlap pilot. Re-pick."
            )
        if p.get("labels"):
            raise SystemExit(
                f"{pid} already has labels {p['labels']!r}; refusing to "
                f"overwrite. Drop it from BRIDGE_LABELS or clear first."
            )

        p["labels"] = vals["labels"]
        p["labeled_by"] = LABELED_BY
        p["labeled_at"] = labeled_at
        p["labeled_against_guide_version"] = GUIDE_VERSION
        p["label_time_seconds"] = vals["label_time_seconds"]
        p["friction_notes"] = list(vals["friction_notes"])

        # Stats.
        total_time += vals["label_time_seconds"]
        n_fn = len(vals["friction_notes"])
        n_friction += n_fn
        n_friction_per_product.append(n_fn)
        layer = (p.get("selection_reason") or "?").split(":")[0]
        by_layer_count[layer] = by_layer_count.get(layer, 0) + 1
        by_layer_friction[layer] = by_layer_friction.get(layer, 0) + n_fn

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

    n = len(BRIDGE_LABELS)
    print(f"Labeled {n} bridge products as {LABELED_BY!r}"
          f" against guide v{GUIDE_VERSION}")
    print(f"Total label time     : {total_time}s  ({total_time / 60:.1f} min)")
    print(f"Mean per product     : {total_time / n:.1f}s")
    print()
    print(f"Friction notes total       : {n_friction}")
    print(f"Mean friction per product  : {n_friction / n:.2f}")
    print(f"Median friction per product: "
          f"{sorted(n_friction_per_product)[n // 2]}")
    print(f"Products with 0 friction   : "
          f"{sum(1 for x in n_friction_per_product if x == 0)} / {n}")
    print()
    print(f"Products with >=1 disagreement vs system : "
          f"{n_disagree_products} / {n}  "
          f"({n_disagree_products / n * 100:.0f}%)")
    print(f"Total per-attribute disagreements        : "
          f"{n_disagreements}  "
          f"({n_disagreements / (n * 4) * 100:.0f}% of slots)")
    print()
    print("by selection layer:")
    for layer in sorted(by_layer_count):
        nn = by_layer_count[layer]
        nf = by_layer_friction[layer]
        print(f"  {layer:<14}  n={nn}  friction_total={nf}  "
              f"mean_friction={nf / nn:.2f}")


if __name__ == "__main__":
    main()
