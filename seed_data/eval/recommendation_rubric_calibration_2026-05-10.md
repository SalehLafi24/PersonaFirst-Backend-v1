# Rubric Calibration Sensitivity Analysis

**Date**: 2026-05-10
**Reviewer**: claude_rubric_v1 (same session that produced rubric v1, v1.5, v2)
**Caveat**: This is NOT an independent second-reviewer pass. It re-rates the
same panel under three explicit interpretations to test whether per-question
results are interpretation-robust or interpretation-bound. A genuinely
independent human reviewer is still required for true cross-validation.

## Interpretation definitions

| q | strict | neutral (matches v2) | lenient |
|---|---|---|---|
| **q1 anchor sense** | Top 3 recs heavily reflect dominant persona; anchor occupies ≥50% of slots. | Anchor visible in top 3 OR ≥30% of slots. | Any rec in top-3 reflects anchor at all. |
| **q2 bizarre items** | Any age_group / use_case mismatch counts. | Out-of-domain (no plausible reason) counts. | Only impossibly-wrong recs count. |
| **q3 complement** | Different product_type AND adds genuine new context (not just shared use_case). | Different product_type from customer's history; shared use_case OK. | Any cross-product_type rec within persona scope counts. |
| **q4 saturation** | Any in-history rec is a flag unless explicitly explained. | Complementary bypass + repurchase-window-out OK. | Only clear policy violations. |
| **q5 surprise** | Genuinely outside customer's history product types. | At least one cross-category rec exists. | Any non-clone rec counts. |

## Per-customer ratings under each interpretation

| Customer | q1 strict / neutral / lenient | q2 strict / neutral / lenient | q3 strict / neutral / lenient | q4 (stable) | q5 strict / neutral / lenient |
|---|---|---|---|---|---|
| baby_essentials | yes / yes / yes | none / none / none | no / partial / yes | yes | no / yes / yes |
| baby_essentials_alt | yes / yes / yes | none / none / none | no / partial / yes | yes | yes / yes / yes |
| toy_focused | no / yes / yes | none / none / none | no / partial / yes | yes | yes / yes / yes |
| book_heavy | yes / yes / yes | none / none / none | yes / yes / yes | yes | yes / yes / yes |
| apparel_focused | yes / yes / yes | none / none / none | yes / yes / yes | yes | yes / yes / yes |
| mixed_category | no / yes / yes | none / none / none | partial / partial / yes | yes | no / yes / yes |
| adult_self | yes / yes / yes | none / none / none | no / partial / partial | yes | no / no / partial |
| gift_buyer | no / no / partial | none / none / none | no / partial / partial | yes | no / no / partial |
| cold_start | yes / yes / yes | none / none / none | partial / partial / yes | yes | yes / yes / yes |
| recent_repurchase | yes / yes / yes | none / none / none | yes / yes / yes | yes | yes / yes / yes |

## Per-question pass-rate ranges

| Question | strict | neutral | lenient | range |
|---|---|---|---|---|
| q1 yes | 7/10 | 9/10 | 9–10/10 | **7–10** (span 3) |
| q2 none | 10/10 | 10/10 | 10/10 | **10** (stable) |
| q3 yes | 3/10 | 3/10 | 8–9/10 | **3–9** (span 6) |
| q4 yes | 10/10 | 10/10 | 10/10 | **10** (stable) |
| q5 yes | 6/10 | 8/10 | 9–10/10 | **6–10** (span 4) |

## Findings

1. **q4 (saturation) and q2 (bizarre) are interpretation-stable.** Both at 10/10 across all interpretations. These are honest engine-quality signals.

2. **q3 (complement) is the most interpretation-sensitive (span 6).** Under a strict reading "complement = different product_type AND different use_case AND new context", only 3 customers pass. Under a lenient reading "any cross-product_type within persona scope", 8–9 customers pass. The same recs are read very differently depending on what "complement" is taken to mean.

3. **q1 and q5 are moderately interpretation-sensitive** (span 3 and 4 respectively). Strict readings emphasise anchor heaviness / surprise novelty; lenient readings allow incidental signal.

4. **The "neutral" column corresponds to my v2 ratings.** It produces the same per-question pass-rates I reported earlier.

## Implications

**The framing of q3 as a "4/10 fail" is interpretation-sensitive, not engine-stable.** An independent reviewer applying the strict definition would also report ~3/10. Applying the lenient definition would report ~8/10. Both are defensible readings of the rubric question as written.

This means:
- The "moments architecture" debate based on q3=3-4/10 may be reading a calibration artifact as an engine deficiency.
- Engine work targeted at "lifting q3" depends on which interpretation we declare authoritative. Different interpretations want different fixes.
- Before any engine work to address q3, the rubric should clarify what "complement" means concretely. Otherwise we'd be optimising for a moving target.

**q4 / q2 at 10/10 stable is the genuine engine-quality signal.** Hard contracts (saturation, no bizarre items) work reliably. The intent layer's RepurchaseSuppression / cross-segment guard / complement bypass work as designed.

**q1 / q5 improvements between v1 and v2 are robust.** Even under strict interpretation, q1 went 6/10 → 7/10 and q5 went 4/10 → 6/10 in the same fix cycles. The cap + guard fixes produced real, interpretation-independent lift.

## Recommendation

Before doing Patch A / B / C or any moments-related work:

1. **Get a genuinely independent reviewer (not me) to apply the rubric.** The cheapest signal we don't have. ~30 min from any other person.
2. **If a second reviewer also lands at q3 strict ~3/10, then sharpen the rubric.** "Complement quality" as currently worded is ambiguous; the strict / neutral / lenient interpretations all defend reasonable answers. A sharper question would produce a tighter range.
3. **Only after rubric calibration:** decide whether q3 indicates engine work, vocabulary work, or it's actually fine.

If a second independent reviewer is not available, the *next* best step is to declare which interpretation we're using, write it into the rubric README, and *then* decide whether engine work is needed. Without that, all q3-driven engine decisions are arguments about wording.
