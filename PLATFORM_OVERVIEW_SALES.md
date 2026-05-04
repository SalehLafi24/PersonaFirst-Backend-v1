# PersonaFirst — Platform Overview for Sales

> Plain-English walkthrough of what the platform does, how it works, and why it wins. Use this as your reference doc for discovery calls, demos, and follow-ups.

---

## 1. The 30-Second Pitch

PersonaFirst is a **personalization engine for retailers**. We turn a brand's product catalog and customer purchase history into ranked, personalized product recommendations that adapt to each shopper in real time.

What makes us different: most recommendation engines treat every product attribute the same — a color is a color, a use case is a use case. We **understand the meaning behind attributes**. We know that "high support" matters for someone who runs marathons but is irrelevant for casual loungewear. That difference is why our recommendations feel like a personal stylist instead of an "other people also bought" widget.

**One-line version:** "We help retailers recommend the right product to the right shopper — using AI that actually understands *why* a product fits."

---

## 2. The Problem We Solve

Every retailer with a catalog of 1,000+ products has the same three problems:

1. **Bad data about products.** Product feeds say "blue cotton t-shirt" — they don't say "casual," "summer," "fits petite frames," "good for layering." Without that meaning, recommendations are shallow.
2. **Shallow personalization.** Most engines look at "what did they click?" or "what did similar people buy?" — they don't capture *why* a customer bought what they bought.
3. **One-size-fits-all logic.** A homepage recommendation, a "you may also like" on a PDP, and a post-purchase email all need different rules — but most engines give you one algorithm with a knob.

The result: recommendations that look generic, conversion lift that plateaus around 1–2%, and a merchandising team that doesn't trust the system.

---

## 3. How PersonaFirst Works (In Plain Language)

There are **four phases** to how a recommendation gets made. Picture it as an assembly line.

### Phase 1 — Understanding the Catalog ("Enrichment")

We take each product and use AI (Anthropic's Claude) to read its name, description, and (eventually) its photos, and extract **rich, meaningful attributes**:

- Not just *"red blouse"* — also *"workwear-appropriate,"* *"flattering for pear shapes,"* *"layers well under blazers,"* *"high-stretch fabric."*
- Each attribute comes with a **confidence score** and **evidence** (the actual text or visual cue we used to make the call). No black box.

**Why sales cares:**
- Our enrichment runs *automatically* on the customer's existing product feed. They don't need to hire a data team or hand-tag thousands of SKUs.
- The evidence trail is gold for objection handling — when merchandisers say "your AI got this wrong," we can show *why* it made that call and let them correct it.

### Phase 2 — Understanding the Shopper ("Affinities")

For each customer, we look at their purchase history and figure out their **preferences across every attribute**:

- "This customer strongly prefers high-support athletic wear, neutral colors, and items suitable for outdoor activities."
- Each preference comes with a **strength score** so we know what matters most to them.

We also compute a **customer signal strength** — how much do we actually know about this person?
- Low signal: they bought one thing once. We need to be cautious.
- High signal: they have rich purchase history. We can be confident and specific.

**Why sales cares:**
- The system **adapts to how much it knows.** New shoppers get safe, popular recommendations. Loyal customers get hyper-personalized picks. Same algorithm, different behavior.
- This is the answer to "how do you handle the cold-start problem?"

### Phase 3 — Learning Patterns ("Relationships")

We watch the entire customer base and learn two kinds of patterns:

1. **What attributes go together.** Customers who like "high support" almost always also want "moisture-wicking." That's a learned relationship — we didn't tell the system, we discovered it from data.
2. **What products go together.** Customers who buy this sports bra also tend to buy these leggings. A product co-purchase graph.

**Why sales cares:**
- This is what powers "complete the look" and cross-category recommendations *without* anyone hand-curating product bundles.
- Patterns are reviewable — merchandisers can approve, reject, or override anything before it goes live.

### Phase 4 — Making the Recommendation ("Scoring & Slots")

When a shopper hits the site, we score every product in the catalog against that shopper using **five signals at once**:

1. **Direct match** — does this product's attributes match what this shopper likes?
2. **Compatibility fit** — is this product *right for them* (e.g., support level, sizing fit)? Mismatches are penalized; matches are amplified.
3. **Pattern match** — does this product fit the patterns we've learned (attribute relationships)?
4. **Behavioral match** — do customers like this one tend to buy this product?
5. **Popularity** — is this a workspace bestseller (used as a tiebreaker)?

We then **combine those signals** with a weighting that the merchandiser controls.

**Why sales cares:** This is the demo moment. Show a prospect a customer profile, then show how the same customer gets different rankings for different "slots" on the site.

---

## 4. The "Slot" Concept — Our Killer Feature

A "slot" is a **placement on the site** — homepage carousel, PDP "you may also like," post-purchase upsell, email module, etc.

Every slot is independently configurable:

- **Different algorithm per slot.** Homepage might be "behavior-first" (what similar shoppers bought). PDP might be "affinity-first" (what matches this shopper's tastes). Email might be "relationship-only" (complementary products to their last purchase).
- **Different filters per slot.** "Only show products under $100." "Only show items in stock." "Only show new arrivals."
- **Cross-slot exclusion.** A product shown in slot 1 is automatically removed from slot 2's eligible pool. No duplicates across the page.
- **Adaptive diversity.** If a shopper has weak signal, we show variety. If they have strong signal, we lean into what they want. Configurable per slot.
- **Smart fallback.** If we don't have enough data to run the requested algorithm, the slot can automatically fall back to a simpler one — instead of returning empty results.

**Why this matters in a deal:**

Most competitors give you one engine and one set of widgets. We give you **the lego pieces to build any personalized surface** — and that's why we land replacement deals against Bloomreach, Dynamic Yield, and Nosto.

---

## 5. Why Our Scoring Is Smarter — In Plain English

This is the technical moat. Here's how to explain it without going into code.

### "Targeting modes" — different attributes, different jobs

Not every attribute should be scored the same way. We classify every attribute into one of four roles:

| Role | Plain English | Example |
|---|---|---|
| **Affinity** | "Does the shopper like this kind of thing?" | Color, brand, material |
| **Compatibility** | "Is this actually right for them?" | Support level, fit type, activity |
| **Filter** | "Hard requirement — must match." | Size, in-stock, price range |
| **Descriptive** | "Just for display, doesn't affect ranking." | SKU, photo angle, marketing copy |

This is the difference between recommending "another red dress because she likes red" and recommending "a high-support sports bra because she runs marathons." **Compatibility attributes carry more weight, and mismatches actively penalize a product** — that's how we avoid embarrassing recommendations.

### "Negative scoring" — knowing when *not* to recommend

If a customer signals they need high-support athletic wear and we surface a low-support bra, that's a bad recommendation — even if the color and brand match. Our engine recognizes this kind of mismatch and **penalizes the score** so it doesn't surface.

Most competitors only do positive matching. They'll recommend a beach dress to a customer who just bought ski gear, because both are "dresses" or "outdoor." We won't.

### "Suppression" — not recommending what they already have

We automatically remove:
- Products the shopper already bought (unless they're repurchasable, like consumables).
- Products in the same group within a sensible repurchase window (you don't recommend a second yoga mat next week).
- Functionally redundant products (don't recommend a second running jacket if they bought one).

Every retailer has been burned by recommending the exact item the shopper just bought. We solve that out of the box.

### "Halo fallback" — graceful recovery

If the engine *would have* recommended a great product, but suppression removed it (already bought), it doesn't just give up — it **boosts other products that share that one's key characteristics.** The shopper still gets relevant recommendations even after their best matches are eliminated.

---

## 6. The Trust Layer — Why Merchandisers Don't Hate Us

Most AI personalization tools fail because the merchandising team doesn't trust them. We solve this in three ways:

### 1. Everything has evidence
Every enriched attribute, every learned relationship, every recommendation comes with **a reason and the data behind it.** No black box.

### 2. Nothing auto-promotes
When our AI proposes a new attribute or a new value (e.g., "I think 'hiking' should be an activity type"), it lands in a **review queue**. A human approves it before it affects rankings. The merch team is in control.

### 3. Per-workspace taxonomy
Every customer (workspace) has their **own** version of the attribute taxonomy. A swimwear brand and an outdoor gear brand can both use PersonaFirst with very different definitions of "activity" or "support level."

---

## 7. The Confidence Score — Knowing How Much to Trust a Recommendation

Every recommendation we return includes a **match confidence score** — a single number that represents how much the system trusts this recommendation, blended from:

- How much we know about the shopper (customer signal strength)
- How well-enriched the product is (product signal strength)
- How many attributes matched
- Whether there were conflicts in the data

**Why this matters for sales:**

This is what enables "smart" surfaces — the customer can choose to *only show recommendations above a confidence threshold,* or to display low-confidence recommendations differently (e.g., "you might also like" vs. "perfect for you"). It's a level of granularity nobody else offers.

---

## 8. What's Inside the Platform (Technically, But Without Jargon)

For sales engineers and technical buyers — the high-level architecture in business terms:

| Layer | What it is | Why prospects ask |
|---|---|---|
| **Catalog enrichment** | AI-powered attribute extraction from product data | "How does the AI part work?" |
| **Customer signals** | Per-customer preference profiles built from purchase history | "How do you handle a new customer?" |
| **Pattern engines** | Two engines: attribute co-occurrence + product co-purchase | "Do you do collaborative filtering?" Yes, plus more. |
| **Scoring engine** | Multi-signal weighted ranker with configurable algorithms | "Can we tune the algorithm?" Yes, per slot. |
| **Slot API** | Recommendation endpoint that takes one or many slot configs and returns ranked results | "How do we integrate?" One API call per page. |
| **Review queues** | Human-in-the-loop approval for proposed taxonomy changes | "How do we keep control?" Everything reviewable. |
| **Multi-tenancy** | Every brand isolated in their own workspace with their own data and taxonomy | "How do you handle multi-brand?" Native support. |

---

## 9. Use Cases — Where We Win

### Use case 1: Apparel with complex fit signals
**Why we win:** We capture compatibility attributes (support level, fit type, body shape, activity) that generic engines flatten. Athletic apparel, intimates, maternity, plus-size — anywhere fit matters.

### Use case 2: Multi-category retailers needing complete-the-look
**Why we win:** Our attribute relationship engine learns cross-category pairings (top + bottom, frame + lens, dress + accessory) without manual rules.

### Use case 3: Brands replacing legacy personalization (Bloomreach, Dynamic Yield, Nosto)
**Why we win:** Slot-based API lets them rebuild every surface with finer control. Modern stack, lower TCO.

### Use case 4: DTC brands that have outgrown Algolia / Shopify Recommend
**Why we win:** They've hit the ceiling of basic similarity — they want personalization. We're the natural upgrade.

### Use case 5: Brands with strong merchandising teams who don't trust AI
**Why we win:** Our review queues, per-workspace taxonomy, and evidence trails put merchandisers in the driver's seat.

---

## 10. Common Objections & How to Answer Them

| Objection | Response |
|---|---|
| *"We already use Bloomreach / Dynamic Yield."* | "Those are great for legacy retailers. We're built for the modern slot-based stack — every surface configurable, AI-enriched attributes out of the box, and a fraction of the implementation time. Want to do a side-by-side?" |
| *"Our product data isn't clean enough for AI."* | "That's exactly why our enrichment phase exists. We turn messy product feeds into rich, typed attributes automatically. Most prospects start the deal thinking they need a data project — they don't." |
| *"How is this different from collaborative filtering?"* | "Collaborative filtering only learns from behavior. We learn from behavior *plus* the actual meaning of products and customer preferences. That's why our recommendations work for sparse-data customers and new products — no cold start." |
| *"We need merchandiser control."* | "Every taxonomy change, every learned pattern, every algorithm weight is human-reviewable and per-workspace configurable. Nothing goes live without merch approval if you want it that way." |
| *"How long does it take to get value?"* | "Two weeks to ingest the catalog and run enrichment. Four to six weeks to a live A/B test. Most customers see 3–8% lift on conversion or revenue per session in their first 90 days." |
| *"What about visual content / image-based attributes?"* | "We have a visual enrichment pathway in the platform — text + image fusion for attributes you can't extract from copy alone. Roadmap conversation we'd love to have." |
| *"Is this just OpenAI / Claude wrapped?"* | "The LLM is one component of one phase. The differentiator is the typed scoring engine, the slot architecture, and the review pipeline — none of which come from an LLM. The LLM enriches data; the platform makes decisions." |

---

## 11. Demo Script — The 10-Minute Walkthrough

1. **Show the customer profile.** "Here's a real shopper — they've bought five items, all athletic, all high-support."
2. **Show the affinity profile.** "Here's what we've learned about her preferences — strength of each."
3. **Show the same customer in two different slots.**
   - Homepage carousel (behavior-first algorithm) — wide variety.
   - PDP "you may also like" (affinity-first) — narrowly matched.
4. **Show the score breakdown for one product.** "This is why this product is ranked #1 — direct match contributed X, behavioral pattern contributed Y, compatibility match contributed Z."
5. **Show evidence on an attribute.** "We marked this product as 'workout-appropriate' because of these phrases in the description."
6. **Show the review queue.** "Here's a new attribute value our AI proposed — your merch team will see this before it affects ranking."
7. **Show the confidence score.** "This recommendation has 0.87 confidence — surface it everywhere. This one has 0.4 — show it only on discovery surfaces."

---

## 12. The 60-Second Close

> "PersonaFirst is the personalization engine for retailers who care about getting the right product in front of the right shopper — not just the most-clicked product. We use AI to understand your catalog at the meaning level, we build deep customer preference profiles, we learn patterns from your data, and we let you configure every personalized surface independently. Your merchandisers stay in control. Your customers see recommendations that feel curated. And you get measurable lift — typically 3–8% on conversion in the first 90 days."

> "Want to plug in your catalog and see what we'd do with it?"

---

*That's the platform.*
