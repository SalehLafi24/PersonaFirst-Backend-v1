// RecommendationCard — one product in the recommendations grid.
//
// Props:
//   rec   { product_id, name, image_url, attributes,
//           explanation: { chips, sentence, strength_dots,
//                          has_warning, warning_reasons },
//           raw: { score, why } }
//   debug bool
//
// Reusable: same card shape works as the recommendation tile in any
// future surface (CMS preview, storefront widget). The integration
// contract is the `rec` prop shape.

import { Component } from "./Component.js";
import { ExplanationChips } from "./ExplanationChips.js";
import { dotPattern, anchorAttributesCaption } from "../explanation.js";

export class RecommendationCard extends Component {
  constructor(parentEl, props) {
    super(parentEl, props);
    this._whyExpanded = false;
  }

  render() {
    const r = this.props.rec;
    const e = (r && r.explanation) || {};
    const root = this.$el("article", { cls: "rec-card" });

    // Image
    const img = this.$el("div", { cls: "rec-image" });
    if (r.image_url) {
      img.appendChild(this.$el("img", { attrs: { src: r.image_url, alt: r.name || "" } }));
    } else {
      const letter = (r.name || r.product_id || "?").trim()[0] || "?";
      img.appendChild(this.$el("div", {
        cls: "rec-image-placeholder",
        text: letter.toUpperCase(),
      }));
    }
    root.appendChild(img);

    // Name
    root.appendChild(this.$el("div", { cls: "rec-name", text: r.name || r.product_id }));

    // Attributes caption
    const caption = anchorAttributesCaption(r.attributes);
    if (caption) {
      root.appendChild(this.$el("div", { cls: "rec-caption", text: caption }));
    }

    // Strength dots
    const dots = this.$el("div", { cls: "rec-strength" });
    dots.appendChild(this.$el("span", {
      cls: "rec-strength-dots",
      text: dotPattern(e.strength_dots || 0),
    }));
    root.appendChild(dots);

    // Per-card warning ribbon (cross-age, gender mismatch, etc.)
    if (e.has_warning && e.warning_reasons && e.warning_reasons.length > 0) {
      root.appendChild(this.$el("div", {
        cls: "rec-warning",
        text: e.warning_reasons.join(" · "),
      }));
    }

    // Chips
    const chipsHost = this.$el("div", { cls: "rec-chips-host" });
    root.appendChild(chipsHost);
    new ExplanationChips(chipsHost, {
      chips: e.chips || [],
      debug: !!this.props.debug,
    }).mount();

    // Why ▸
    const whyToggle = this.$el("button", {
      cls: "rec-why-toggle",
      attrs: { type: "button" },
      text: this._whyExpanded ? "Why ▾" : "Why ▸",
    });
    whyToggle.addEventListener("click", () => {
      this._whyExpanded = !this._whyExpanded;
      this.update();
    });
    root.appendChild(whyToggle);

    if (this._whyExpanded) {
      const panel = this.$el("div", { cls: "rec-why-panel" });
      panel.appendChild(this.$el("p", {
        cls: "rec-why-sentence",
        text: e.sentence || "Cohort-only match.",
      }));
      if (this.props.debug && r.raw) {
        const dbg = this.$el("pre", { cls: "rec-why-debug" });
        const lines = [
          `score: ${(r.raw.score ?? 0).toFixed(4)}`,
          `why:`,
        ];
        for (const w of (r.raw.why || [])) {
          const c = (typeof w.contribution === "number") ? w.contribution.toFixed(4) : "0.0000";
          lines.push(`  - ${w.component}  (${c})`);
        }
        dbg.textContent = lines.join("\n");
        panel.appendChild(dbg);
      }
      root.appendChild(panel);
    }

    return root;
  }
}
