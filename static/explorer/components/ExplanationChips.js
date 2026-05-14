// ExplanationChips — renders the chip row for a recommendation.
//
// Props:
//   chips    [{ kind, label, tone, debug_only? }]
//   debug    bool — when true, debug-only chips are shown too
//
// Reusable: same chip row appears under any recommended product in the
// explorer, a CMS preview, or a storefront widget.

import { Component } from "./Component.js";
import { chipToneClass, visibleChips } from "../explanation.js";

export class ExplanationChips extends Component {
  render() {
    const root = this.$el("div", { cls: "chips" });
    const chips = visibleChips(
      { chips: this.props.chips || [] },
      { debug: !!this.props.debug },
    );
    if (chips.length === 0) {
      root.appendChild(this.$el("span", {
        cls: "chip chip-neutral chip-empty",
        text: "Cohort match",
      }));
      return root;
    }
    for (const c of chips) {
      const el = this.$el("span", { cls: chipToneClass(c.tone), text: c.label });
      root.appendChild(el);
    }
    return root;
  }
}
