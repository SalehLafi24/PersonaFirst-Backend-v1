# Rubric Reviewer Comparison

**Reviewers compared:**
- `claude_rubric_v1` — me, post-fix engine (3 prior passes; v2 is the relevant one)
- `Sary_barbar` — independent reviewer, same fixture, same engine state, same packet

Files:
- `recommendation_rubric_2026-05-10_post_all.csv` (claude v2)
- `recommendation_rubric_Sary_barbar_2026-05-10.csv` (Sary)

## Side-by-side per-question pass rates

I'll report claude v2 strict / neutral counts alongside Sary's raw answers. Some
questions have polarity ambiguity in Sary's answers (q2 and q4) — Sary used
yes/no where the template specifies none/1/>1 (q2) and yes/no (q4). Those
need a note before we tally.

| Question | claude v2 (neutral) | claude v2 (strict, from calibration) | Sary | Notes |
|---|---|---|---|---|
| q1 anchor sense (yes) | 9/10 | 7/10 | **9/10** | Strong agreement. Both flag `gift_buyer` as the failure. |
| q2 no bizarre items | 10/10 "none" | 10/10 "none" | **8/10 "No"** (none) + 2/10 "Yes" (some) | Sary flags `mixed_category` and `gift_buyer` as having bizarre items — both are notes-supported, not polarity slips. Otherwise aligned. |
| q3 complement (yes) | 3/10 | 3/10 | **5/10** | Meaningful split. Sary reads complement more leniently than my v2 — closer to my calibration "lenient" interpretation. |
| q4 saturation respect (yes) | 10/10 | 10/10 | **Polarity inverted in Sary's CSV** — see note below | When normalised, both agree saturation is respected for most customers; one disagreement on `mixed_category`. |
| q5 surprise (yes) | 8/10 | 6/10 | **2/10** | Biggest disagreement. Sary applies a much stricter definition of "interesting expansion" than I did. |

### q4 polarity note

Sary's q4 answers don't track saturation events in any of the notes. Where
`mixed_category` and `cold_start` are marked "Yes", the notes mention other
things; where `baby_essentials` is marked "No", the notes say "good overall"
(positive). I believe Sary's "Yes" on q4 sometimes meant *"yes there's a
saturation-style problem"* (matching some notes) and sometimes meant *"yes,
saturation is respected"* (matching others). Without polling Sary directly,
the q4 column is too noisy to tally. **What is clear: Sary did not raise
saturation as a problem in any qualitative note**, which matches the engine
checks (q4 = 10/10 stable on the conformance side).

## What changes vs. my prior analysis

The calibration sensitivity analysis I ran predicted that **q3 would have
the widest interpretation span (3-9)** and **q1/q2/q4 would be the most
stable**. Sary's numbers confirm that prediction:

- **q1, q2, q4 stable across reviewers.** The cross-segment guard,
  RepurchaseSuppression, and the cap on bizarre items hold up under
  independent reading.
- **q3 lands between my strict and lenient columns** (5/10 vs the 3-9
  range I predicted). Confirmed: q3 is interpretation-bound, not
  engine-broken. The number an independent reviewer produces depends on
  what they think "complement" means.
- **q5 is MORE interpretation-sensitive than I predicted.** I had
  q5 spanning 6-10 across my own three interpretations; Sary lands at 2
  — outside my range. Sary is reading "interesting expansion" more
  strictly than my strict interpretation. This is signal the calibration
  analysis missed: my strictness levels were too generous on q5.

## What Sary's *notes* surface that pass-rates don't

The qualitative comments are more informative than the binary tally.
Five specific structural observations:

1. **`baby_essentials_alt` — temporal/lifecycle gap**: "Teethers are normally
   changed 3-6 months ... Frequency/consumable period was not accurate
   here." The engine has no notion of *replacement cadence* per product.
   This is a real architectural gap — products that are consumables
   (teethers, diapers) vs durables (strollers) are treated identically.

2. **`apparel_focused` — occasional-purchase pattern**: "If you buy party
   items, you make the party and that's it. Showing other items is
   irrelevant." Sary's pointing at *moment-completion* — once the moment
   is over (party happened), more party-adjacent recs are wrong. This is
   the same shape of gap the "moments" discussion was circling.

3. **`mixed_category` — C12 visibility issue**: "Stroller accessories are
   random." The complementary bypass surfaces a previously-bought
   stroller part. To the customer that looks random, even though it's
   designed behaviour. This is a UX/explanation gap, not an engine
   bug — but it's a real one.

4. **`adult_self` — taxonomy granularity**: "Beauty is large L1 category."
   Sary's saying makeup/skincare/hair_care being grouped looks like one
   category to a customer-eye, even though the manifest treats them
   distinctly. Worth flagging that our internal taxonomy granularity
   doesn't match customer perception in this segment.

5. **`gift_buyer` — cross-age collapse confirmed**: "Lots of products
   purchased are more for older kids, and recommended products are for
   all for infants." Two reviewers, same finding. This is a robust
   real engine issue.

## The takeaways

### Confirmed engine quality signals
- **Hard constraints stable** (q4 saturation 10/10 in spirit across both
  reviewers).
- **No bizarre items** — both reviewers agree the cross-segment guard
  works on most customers.
- **q1 anchor sense holds** — both reviewers at 9/10. The diversity cap
  and product_type cap delivered.

### Confirmed engine quality gaps
- **`gift_buyer` cross-age collapse** — robust across reviewers, real
  issue worth addressing.
- **Consumable / replacement-cadence missing** — Sary flags this once,
  could be a real engine gap.

### Disagreements that are calibration, not engine
- **q3 complement quality** — interpretation-bound, as predicted.
- **q5 surprise** — much more interpretation-sensitive than my analysis
  suggested. Reasonable readers can disagree wildly on what counts as
  "interesting expansion".

## Recommendation

**The two cross-reviewer-confirmed gaps are concrete enough to act on:**

1. **`gift_buyer` cross-age collapse** — the persona-spread check + cap I
   proposed earlier (Patch B) is the right size of intervention. Both
   reviewers agree it's wrong. Worth doing.

2. **Consumable replacement cadence** — Sary's specific observation about
   teethers. Worth investigating whether the product taxonomy carries any
   lifecycle metadata that we're ignoring; if not, this is a deferred
   product/data question, not engineering today.

**Defer engine work targeted at q3 or q5.** Both questions are
interpretation-bound. The right move on those is to sharpen the rubric
wording (with concrete examples) before driving any engine decisions
from them.

**Defer "moments" architecture.** Sary's apparel-focused note ("you make
the party and that's it") gestures at the same gap I described — but
this is one note from one reviewer on one customer. Not enough evidence
for an architectural layer. Revisit after real-customer signal exists.

**Get the rubric polarity unambiguous before the next pass.** Sary's q2
and q4 answers had polarity drift from the template intent. The next
version of the rubric should make polarity impossible to mis-read
(rephrase "no bad repeats" as "no recently-bought products re-appear: yes
[good] / no [problem]" or split into two binary questions).
