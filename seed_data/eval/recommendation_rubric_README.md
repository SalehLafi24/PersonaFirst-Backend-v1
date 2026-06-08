# Recommendation Rubric — Human Review Pass

**Purpose:** the human-judgment residue. Catches "all conformance checks pass but the
recs feel wrong" cases. NOT graded — produces a per-question pass-rate, not a score.

## When to run

- After every meaningful change to the rec engine, persona, intent layer, or manifest weights.
- When the conformance matrix from `scripts/recommendation_eval.py` has flipped pass/fail.
- ~30 minutes per pass for the 10-customer panel.

## Inputs

- `recommendation_customers.json` — fixture pinned to a workspace.
- A live rec engine. Generate the inspection table by running per customer:

  ```
  python scripts/recommendation_eval.py --workspace mumzworld_v3_sample --customer <id> --verbose
  ```

  Then look at the rec list for that customer (you may want to dump it via a small helper
  that prints `rec.product_id, rec.name, rec.attributes, rec.intent_contributions`).

## Rubric — 5 questions per customer

For each customer, answer each question. Three-state for q2/q3, binary for q1/q4/q5.

**Polarity — read this first.** Every question is worded so that **`yes`
(or `none` for q2) is always the good answer** and **`no` (or a non-zero
count) always flags a problem**. No question is inverted: you never answer
"yes" to report something wrong. If your instinct is "yes, there's a problem
here", that maps to the **`no`** column.

| Q | Question (yes / none = good) | Values | ✓ good vs ✗ problem |
|---|---|---|---|
| q1 | **Anchor sense**: do the recs reflect the customer's dominant persona / shopping pattern? | yes / no | ✓ yes · ✗ no = the dominant signal isn't reflected at all |
| q2 | **No bizarre items**: how many recs are clearly absurd for this persona? (a count, not yes/no) | none / 1 / >1 | ✓ none · ✗ 1 or >1 — counts items, not severity |
| q3 | **Complement quality**: do complement-style recs genuinely complement rather than duplicate? | yes / partial / no | ✓ yes · partial = some complements are echoes · ✗ no = clones / near-dupes |
| q4 | **Saturation respect**: are recently-purchased items kept out of the recs (or clearly justified when shown)? | yes / no | ✓ yes = respected · ✗ no = an unjustified repeat appeared (cross-check `intent_contributions`) |
| q5 | **Surprise**: is there at least one interesting-but-defensible rec? | yes / no | ✓ yes · ✗ no = everything is predictable (echo chamber) |

## Filling the template

`recommendation_rubric_template.csv` has one row per customer. Fill the columns
`q1_anchor_sense, q2_no_bizarre_items, q3_complement_quality, q4_saturation_respect, q5_surprise`
plus `notes` for any specifics, and metadata (`reviewer`, `reviewed_at`, `git_sha`).

Save the completed file as `recommendation_rubric_<date>.csv` in the same dir; do NOT
overwrite the template.

## What "passes" the rubric

There is no aggregate pass score. The rubric exposes per-question rates. A useful
state of the world looks like:

- q1 (anchor sense) at 9/10 or 10/10.
- q2 (no bizarre items) "none" for ≥ 8/10.
- q3 (complement quality) "yes" for ≥ 7/10.
- q4 (saturation) at 10/10. (q4 < 10/10 means RepurchaseSuppression has a bug — this
  is exactly what the constraint check `no_purchased_in_response` is also catching.)
- q5 (surprise) ≥ 5/10.

Lower than these is a signal something is off — either rec engine, persona extraction,
or the fixture itself doesn't represent what we thought it did.

## What NOT to do

- Don't optimize the rec engine to pass the rubric. The rubric is a residue check, not
  a target.
- Don't average q1..q5 into a single score.
- Don't replace this human review with an LLM. The rubric exists precisely because
  conformance + LLM grading can't catch the "feels wrong" residue.
- Don't extend the rubric beyond 5 questions without strong reason. More questions =
  longer review = stale results.

## When to revise the rubric

If rubric questions consistently produce the same answer for every customer, the
question isn't differentiating. Replace it with a question that does.
