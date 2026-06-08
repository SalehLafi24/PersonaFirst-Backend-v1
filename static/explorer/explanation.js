// Client-side explanation helpers.
//
// The backend `explanation_transformer` is the source of truth — every
// recommendation already arrives with chips, sentence, and strength_dots
// computed. This module is a thin layer that:
//
//   - hides debug-only chips when not in debug mode
//   - renders the strength dot pattern (●●●●○)
//   - composes the per-card "Why" panel
//
// Reusable: a future storefront widget can call exactly the same backend
// route and import this file to render the same visual language.

export function visibleChips(explanation, { debug = false } = {}) {
  if (!explanation || !explanation.chips) return [];
  return explanation.chips.filter((c) => debug || !c.debug_only);
}

export function dotPattern(strengthDots) {
  const n = Math.max(0, Math.min(5, Number(strengthDots) || 0));
  return "●".repeat(n) + "○".repeat(5 - n);
}

export function chipToneClass(tone) {
  switch (tone) {
    case "positive": return "chip chip-positive";
    case "warning":  return "chip chip-warning";
    default:         return "chip chip-neutral";
  }
}

// Pure helper: build a one-line caption for the anchor card
// (e.g. "changing_mat · infant · unisex"). Returns "" if no attrs.
export function anchorAttributesCaption(attrs) {
  if (!attrs) return "";
  const parts = [];
  for (const k of ["product_type", "age_group", "gender"]) {
    const v = attrs[k];
    if (v) parts.push(v);
  }
  return parts.join(" · ");
}
