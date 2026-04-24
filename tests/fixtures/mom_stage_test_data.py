"""Realistic test dataset for mom_stage attribute enrichment.

Products span postpartum, pregnancy, nursing, mixed, and control contexts.
MODEL_OUTPUTS follow the extraction rules: allowed_values is empty, so all
detections go into proposed_values with values always [].
"""

PRODUCTS = [
    {
        "product_id": "P101",
        "sku": "BRA-NURSE-BLK",
        "name": "Hands-Free Nursing Bra",
        "category": "bras",
        "description": (
            "Black clip-down nursing bra designed for breastfeeding moms. "
            "One-handed clasp for easy access during feeding. Soft modal "
            "fabric with light compression for postpartum comfort."
        ),
        "catalog_attributes": {"material": "modal blend", "fit": "regular"},
    },
    {
        "product_id": "P102",
        "sku": "LEG-PREG-NVY",
        "name": "Over-Belly Maternity Leggings",
        "category": "leggings",
        "description": (
            "Navy over-belly maternity leggings with a full-panel waistband "
            "that stretches across the bump. Designed for second and third "
            "trimester wear. Soft brushed interior and no-dig seams."
        ),
        "catalog_attributes": {"material": "brushed knit", "fit": "maternity"},
    },
    {
        "product_id": "P103",
        "sku": "TOP-PP-GRY",
        "name": "Postpartum Recovery Tank",
        "category": "tops",
        "description": (
            "Grey postpartum recovery tank with built-in abdominal support "
            "panel. Helps with core recovery after C-section or vaginal "
            "delivery. Moisture-wicking fabric for new-mom life."
        ),
        "catalog_attributes": {"material": "nylon spandex", "fit": "compression"},
    },
    {
        "product_id": "P104",
        "sku": "SET-NWBRN-PNK",
        "name": "Newborn Skin-to-Skin Wrap Top",
        "category": "tops",
        "description": (
            "Blush pink wrap top engineered for skin-to-skin contact with "
            "your newborn. Stretchy crossover front holds baby securely "
            "against the chest. Also suitable for early nursing sessions."
        ),
        "catalog_attributes": {"material": "bamboo jersey", "fit": "regular"},
    },
    {
        "product_id": "P105",
        "sku": "LEG-PPMIX-BLK",
        "name": "4th Trimester Transition Leggings",
        "category": "leggings",
        "description": (
            "Black high-waist leggings designed to work from late pregnancy "
            "through early postpartum. Adjustable fold-over waistband "
            "accommodates a growing bump and supports recovery. Gentle "
            "compression for swelling relief."
        ),
        "catalog_attributes": {"material": "double knit", "fit": "slim"},
    },
    {
        "product_id": "P106",
        "sku": "BRA-SPORT-RED",
        "name": "High Impact Running Bra",
        "category": "bras",
        "description": (
            "Red high impact sports bra with maximum support for running "
            "and HIIT. Adjustable straps and encapsulated cups for bounce "
            "control. Mesh back panel for ventilation."
        ),
        "catalog_attributes": {"material": "nylon blend", "fit": "compression"},
    },
    {
        "product_id": "P107",
        "sku": "JKT-TRAVEL-BGE",
        "name": "Packable Travel Jacket",
        "category": "jackets",
        "description": (
            "Beige packable jacket for airport layering and light outdoor "
            "use. Wrinkle-resistant shell fabric. Packs into its own pocket."
        ),
        "catalog_attributes": {"material": "nylon", "fit": "regular"},
    },
    {
        "product_id": "P108",
        "sku": "BRA-PNURSE-NVY",
        "name": "Prenatal & Nursing Support Bra",
        "category": "bras",
        "description": (
            "Navy wire-free bra with expandable cups for changing bust size "
            "during pregnancy. Converts to a nursing bra with drop-down "
            "clips after delivery. Wide under-band for rib cage expansion."
        ),
        "catalog_attributes": {"material": "organic cotton", "fit": "regular"},
    },
]


MODEL_OUTPUTS = {
    # ------------------------------------------------------------------
    # P101 — Hands-Free Nursing Bra
    # Strong nursing + postpartum signals
    # ------------------------------------------------------------------
    ("P101", "mom_stage"): {
        "attribute_name": "mom_stage",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "nursing",
                "confidence": 0.97,
                "evidence": [
                    '"nursing bra designed for breastfeeding moms"',
                    '"One-handed clasp for easy access during feeding"',
                ],
            },
            {
                "value": "postpartum",
                "confidence": 0.88,
                "evidence": [
                    '"light compression for postpartum comfort"',
                ],
            },
        ],
        "warnings": [],
    },
    # ------------------------------------------------------------------
    # P102 — Over-Belly Maternity Leggings
    # Strong pregnancy signal
    # ------------------------------------------------------------------
    ("P102", "mom_stage"): {
        "attribute_name": "mom_stage",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "pregnancy",
                "confidence": 0.97,
                "evidence": [
                    '"maternity leggings with a full-panel waistband that stretches across the bump"',
                    '"Designed for second and third trimester wear"',
                ],
            },
        ],
        "warnings": [],
    },
    # ------------------------------------------------------------------
    # P103 — Postpartum Recovery Tank
    # Strong postpartum signal
    # ------------------------------------------------------------------
    ("P103", "mom_stage"): {
        "attribute_name": "mom_stage",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "postpartum",
                "confidence": 0.98,
                "evidence": [
                    '"postpartum recovery tank with built-in abdominal support panel"',
                    '"Helps with core recovery after C-section or vaginal delivery"',
                ],
            },
        ],
        "warnings": [],
    },
    # ------------------------------------------------------------------
    # P104 — Newborn Skin-to-Skin Wrap Top
    # Strong newborn signal, secondary nursing signal
    # ------------------------------------------------------------------
    ("P104", "mom_stage"): {
        "attribute_name": "mom_stage",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "newborn",
                "confidence": 0.96,
                "evidence": [
                    '"engineered for skin-to-skin contact with your newborn"',
                    '"Stretchy crossover front holds baby securely against the chest"',
                ],
            },
            {
                "value": "nursing",
                "confidence": 0.85,
                "evidence": [
                    '"Also suitable for early nursing sessions"',
                ],
            },
        ],
        "warnings": [],
    },
    # ------------------------------------------------------------------
    # P105 — 4th Trimester Transition Leggings
    # Mixed: pregnancy + postpartum
    # ------------------------------------------------------------------
    ("P105", "mom_stage"): {
        "attribute_name": "mom_stage",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "pregnancy",
                "confidence": 0.93,
                "evidence": [
                    '"designed to work from late pregnancy through early postpartum"',
                    '"accommodates a growing bump"',
                ],
            },
            {
                "value": "postpartum",
                "confidence": 0.92,
                "evidence": [
                    '"designed to work from late pregnancy through early postpartum"',
                    '"supports recovery"',
                ],
            },
        ],
        "warnings": [],
    },
    # ------------------------------------------------------------------
    # P106 — High Impact Running Bra
    # Control: no maternity context
    # ------------------------------------------------------------------
    ("P106", "mom_stage"): {
        "attribute_name": "mom_stage",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [],
        "warnings": ["no_supported_value_found"],
    },
    # ------------------------------------------------------------------
    # P107 — Packable Travel Jacket
    # Control: no maternity context
    # ------------------------------------------------------------------
    ("P107", "mom_stage"): {
        "attribute_name": "mom_stage",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [],
        "warnings": ["no_supported_value_found"],
    },
    # ------------------------------------------------------------------
    # P108 — Prenatal & Nursing Support Bra
    # Mixed: pregnancy + nursing
    # ------------------------------------------------------------------
    ("P108", "mom_stage"): {
        "attribute_name": "mom_stage",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "pregnancy",
                "confidence": 0.95,
                "evidence": [
                    '"expandable cups for changing bust size during pregnancy"',
                    '"Wide under-band for rib cage expansion"',
                ],
            },
            {
                "value": "nursing",
                "confidence": 0.93,
                "evidence": [
                    '"Converts to a nursing bra with drop-down clips after delivery"',
                ],
            },
        ],
        "warnings": [],
    },
}
