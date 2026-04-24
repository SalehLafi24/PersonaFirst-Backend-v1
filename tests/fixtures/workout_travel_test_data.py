PRODUCTS = [
    {
        "product_id": "PX01",
        "name": "HIIT Performance Bra",
        "description": "Black high-support sports bra built for HIIT, box jumps, and plyometric circuits. Bonded seams eliminate chafing during high-rep explosive movements. Compression fit with racerback.",
        "attributes": {"category": "bras"},
    },
    {
        "product_id": "PX02",
        "name": "Sprint Interval Tights",
        "description": "Navy compression tights engineered for sprint intervals and track repeats. Graduated compression supports fast recovery between sets. Reflective details for dawn sessions.",
        "attributes": {"category": "leggings"},
    },
    {
        "product_id": "PX03",
        "name": "CrossFit Training Tee",
        "description": "Grey sweat-wicking tee designed for CrossFit WODs and high-intensity functional training. Reinforced shoulder seams for barbell work. Dropped hem stays tucked during burpees.",
        "attributes": {"category": "tops"},
    },
    {
        "product_id": "PX04",
        "name": "Studio Cycling Leggings",
        "description": "Black mid-weight leggings for indoor cycling and spin class. Moderate compression with flatlock seams for saddle comfort. Moisture-wicking gusset for sustained moderate effort.",
        "attributes": {"category": "leggings"},
    },
    {
        "product_id": "PX05",
        "name": "Trail Pace Hoodie",
        "description": "Olive lightweight hoodie for moderate-effort hiking and fast-packing. Merino blend manages temperature across sustained aerobic effort. Packs into hood pocket.",
        "attributes": {"category": "tops"},
    },
    {
        "product_id": "PX11",
        "name": "Explosive Training Shorts",
        "description": "Lightweight shorts designed for HIIT workouts, box jumps, and explosive circuit training. Built for high-intensity intervals.",
        "attributes": {"category": "shorts"}
    },
    {
        "product_id": "PX12",
        "name": "Plyometric Performance Tank",
        "description": "Breathable tank for plyometric drills and HIIT sessions. Supports fast, explosive movement during high-intensity training.",
        "attributes": {"category": "tops"}
    },
    {
        "product_id": "PX13",
        "name": "Marathon Distance Tights",
        "description": "Compression tights built for long-distance running and endurance sessions. Ideal for marathon training and steady pacing.",
        "attributes": {"category": "leggings"}
    },
    {
        "product_id": "PX14",
        "name": "Lightweight Running Windbreaker",
        "description": "Ultra-light windbreaker designed for running in variable weather. Packs easily and stays breathable during runs.",
        "attributes": {"category": "jackets"}
    },
    {
        "product_id": "PX15",
        "name": "Studio Yoga Bra",
        "description": "Soft support bra designed for yoga, stretching, and low-impact studio sessions. Focused on flexibility and comfort.",
        "attributes": {"category": "bras"}
    },
    {
        "product_id": "PX16",
        "name": "Flow Yoga Leggings",
        "description": "Stretch leggings optimized for yoga flow and mobility work. Designed for low-intensity movement and balance.",
        "attributes": {"category": "leggings"}
    },
    {
        "product_id": "PX17",
        "name": "Trail Hiking Pants",
        "description": "Durable pants for hiking and trail exploration. Built for long outdoor walks and rugged terrain.",
        "attributes": {"category": "pants"}
    },
    {
        "product_id": "PX18",
        "name": "All-Weather Hiking Jacket",
        "description": "Protective jacket for hiking in changing weather. Breathable and durable for extended outdoor activity.",
        "attributes": {"category": "jackets"}
    },
    {
        "product_id": "PX19",
        "name": "Backpacking Base Layer",
        "description": "Merino base layer designed for multi-day backpacking trips. Optimized for carrying loads and extended outdoor travel.",
        "attributes": {"category": "tops"}
    },
    {
        "product_id": "PX20",
        "name": "Minimal Everyday Tank",
        "description": "Simple cotton tank for casual daily wear. Clean silhouette with soft fabric.",
        "attributes": {"category": "tops"},
    },
    {
        "product_id": "PX06",
        "name": "Yin Yoga Flow Pants",
        "description": "Mauve wide-leg pants for yin yoga and restorative practice. Ultra-soft brushed fabric with no compression. Relaxed waistband sits gently without restriction.",
        "attributes": {"category": "leggings"},
    },
    {
        "product_id": "PX07",
        "name": "Recovery Lounge Set",
        "description": "Light grey matching lounge set for post-workout recovery and rest days. Loose silhouette and pill-resistant fleece. Designed to be worn between training sessions.",
        "attributes": {"category": "sets"},
    },
    {
        "product_id": "PX08",
        "name": "Airport Layer Jacket",
        "description": "Beige packable travel jacket that stuffs into its own pocket. Wrinkle-free shell transitions from plane to street. Zippered security pocket for passport and boarding pass.",
        "attributes": {"category": "jackets"},
    },
    {
        "product_id": "PX09",
        "name": "Travel-Day Slim Pants",
        "description": "Navy slim travel pants with wrinkle-resistant stretch fabric. Four-way stretch for long-haul comfort. Quick-dry after sink wash. Doubles as smart-casual dinner pants.",
        "attributes": {"category": "leggings"},
    },
    {
        "product_id": "PX10",
        "name": "Everyday Scoop Bralette",
        "description": "White scoop-neck bralette for daily wear. Soft ribbed cotton with no underwire. Simple pullover design.",
        "attributes": {"category": "bras"},
    },
]


MODEL_OUTPUTS = {
    ("PX01", "workout_intensity"): {
        "attribute_name": "workout_intensity",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "high",
                "confidence": 0.96,
                "evidence": [
                    "\"high-support sports bra\"",
                    "\"high-rep explosive movements\"",
                ],
            },
            {
                "value": "hiit",
                "confidence": 0.95,
                "evidence": [
                    "\"built for HIIT, box jumps, and plyometric circuits\"",
                ],
            },
        ],
        "warnings": [],
    },
    ("PX01", "travel_friendly"): {
        "attribute_name": "travel_friendly",
        "attribute_class": "compatibility",
        "values": [
            {
                "value": "low",
                "confidence": 0.85,
                "evidence": [
                    "No travel-specific features; sport-only construction",
                ],
                "reasoning_mode": "suitability",
            },
        ],
        "proposed_values": [],
        "warnings": [],
    },
    ("PX02", "workout_intensity"): {
        "attribute_name": "workout_intensity",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "high",
                "confidence": 0.95,
                "evidence": [
                    "\"engineered for sprint intervals and track repeats\"",
                ],
            },
        ],
        "warnings": [],
    },
    ("PX02", "travel_friendly"): {
        "attribute_name": "travel_friendly",
        "attribute_class": "compatibility",
        "values": [
            {
                "value": "low",
                "confidence": 0.82,
                "evidence": [
                    "Sport-specific compression; no packability or wrinkle-resistance mentioned",
                ],
                "reasoning_mode": "suitability",
            },
        ],
        "proposed_values": [],
        "warnings": [],
    },
    ("PX03", "workout_intensity"): {
        "attribute_name": "workout_intensity",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "high",
                "confidence": 0.97,
                "evidence": [
                    "\"designed for CrossFit WODs and high-intensity functional training\"",
                ],
            },
            {
                "value": "hiit",
                "confidence": 0.93,
                "evidence": [
                    "\"high-intensity functional training\"",
                    "\"burpees\"",
                ],
            },
        ],
        "warnings": [],
    },
    ("PX03", "travel_friendly"): {
        "attribute_name": "travel_friendly",
        "attribute_class": "compatibility",
        "values": [
            {
                "value": "low",
                "confidence": 0.80,
                "evidence": [
                    "Gym-specific construction; no travel features",
                ],
                "reasoning_mode": "suitability",
            },
        ],
        "proposed_values": [],
        "warnings": [],
    },
    ("PX04", "workout_intensity"): {
        "attribute_name": "workout_intensity",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "moderate",
                "confidence": 0.92,
                "evidence": [
                    "\"indoor cycling and spin class\"",
                    "\"sustained moderate effort\"",
                ],
            },
        ],
        "warnings": [],
    },
    ("PX04", "travel_friendly"): {
        "attribute_name": "travel_friendly",
        "attribute_class": "compatibility",
        "values": [
            {
                "value": "low",
                "confidence": 0.83,
                "evidence": [
                    "Studio-specific; no packability or wrinkle-resistance",
                ],
                "reasoning_mode": "suitability",
            },
        ],
        "proposed_values": [],
        "warnings": [],
    },
    ("PX05", "workout_intensity"): {
        "attribute_name": "workout_intensity",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "moderate",
                "confidence": 0.90,
                "evidence": [
                    "\"moderate-effort hiking and fast-packing\"",
                    "\"sustained aerobic effort\"",
                ],
            },
        ],
        "warnings": [],
    },
    ("PX05", "travel_friendly"): {
        "attribute_name": "travel_friendly",
        "attribute_class": "compatibility",
        "values": [
            {
                "value": "medium",
                "confidence": 0.88,
                "evidence": [
                    "\"Packs into hood pocket\"",
                    "\"lightweight hoodie\"",
                ],
                "reasoning_mode": "suitability",
            },
        ],
        "proposed_values": [],
        "warnings": [],
    },
    ("PX06", "workout_intensity"): {
        "attribute_name": "workout_intensity",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "low",
                "confidence": 0.94,
                "evidence": [
                    "\"yin yoga and restorative practice\"",
                    "\"no compression\"",
                ],
            },
        ],
        "warnings": [],
    },
    ("PX06", "travel_friendly"): {
        "attribute_name": "travel_friendly",
        "attribute_class": "compatibility",
        "values": [
            {
                "value": "low",
                "confidence": 0.84,
                "evidence": [
                    "Wide-leg construction; no packability or wrinkle-resistance",
                ],
                "reasoning_mode": "suitability",
            },
        ],
        "proposed_values": [],
        "warnings": [],
    },
    ("PX07", "workout_intensity"): {
        "attribute_name": "workout_intensity",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [
            {
                "value": "low",
                "confidence": 0.91,
                "evidence": [
                    "\"post-workout recovery and rest days\"",
                    "\"Designed to be worn between training sessions\"",
                ],
            },
        ],
        "warnings": [],
    },
    ("PX07", "travel_friendly"): {
        "attribute_name": "travel_friendly",
        "attribute_class": "compatibility",
        "values": [
            {
                "value": "low",
                "confidence": 0.81,
                "evidence": [
                    "Lounge-specific; pill-resistant fleece not travel-oriented",
                ],
                "reasoning_mode": "suitability",
            },
        ],
        "proposed_values": [],
        "warnings": [],
    },
    ("PX08", "workout_intensity"): {
        "attribute_name": "workout_intensity",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [],
        "warnings": ["no_supported_value_found"],
    },
    ("PX08", "travel_friendly"): {
        "attribute_name": "travel_friendly",
        "attribute_class": "compatibility",
        "values": [
            {
                "value": "high",
                "confidence": 0.97,
                "evidence": [
                    "\"packable travel jacket that stuffs into its own pocket\"",
                    "\"Wrinkle-free shell transitions from plane to street\"",
                    "\"Zippered security pocket for passport and boarding pass\"",
                ],
                "reasoning_mode": "suitability",
            },
        ],
        "proposed_values": [],
        "warnings": [],
    },
    ("PX09", "workout_intensity"): {
        "attribute_name": "workout_intensity",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [],
        "warnings": ["no_supported_value_found"],
    },
    ("PX09", "travel_friendly"): {
        "attribute_name": "travel_friendly",
        "attribute_class": "compatibility",
        "values": [
            {
                "value": "high",
                "confidence": 0.96,
                "evidence": [
                    "\"wrinkle-resistant stretch fabric\"",
                    "\"Quick-dry after sink wash\"",
                    "\"Doubles as smart-casual dinner pants\"",
                ],
                "reasoning_mode": "suitability",
            },
        ],
        "proposed_values": [],
        "warnings": [],
    },
    ("PX10", "workout_intensity"): {
        "attribute_name": "workout_intensity",
        "attribute_class": "contextual_semantic",
        "values": [],
        "proposed_values": [],
        "warnings": ["no_supported_value_found"],
    },
    ("PX10", "travel_friendly"): {
        "attribute_name": "travel_friendly",
        "attribute_class": "compatibility",
        "values": [],
        "proposed_values": [],
        "warnings": ["no_supported_value_found"],
    },
}
