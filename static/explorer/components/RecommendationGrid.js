// RecommendationGrid — renders the list of recommendation cards plus
// the weak-case banner (empty / partial / no recs).
//
// Props:
//   tier               raw tier string from the API
//   tier_label_human   demo-mode label
//   recommendations    array of recs (see RecommendationCard.props.rec)
//   debug              bool
//   demoMode           bool — when true, drop negative-score recs
//
// Reusable: same grid + weak-case logic powers any surface.

import { Component } from "./Component.js";
import { RecommendationCard } from "./RecommendationCard.js";

export class RecommendationGrid extends Component {
  render() {
    const root = this.$el("section", { cls: "rec-grid-section" });

    const tier = this.props.tier || "empty";
    let recs = this.props.recommendations || [];

    // Demo mode: hide negative-score recs from the visible grid (still
    // available in the raw response for debug). Keeps weak-case noise
    // out of the demo without changing the V1 contract.
    if (this.props.demoMode) {
      recs = recs.filter((r) => (r.raw && r.raw.score >= 0));
    }

    // Weak-case banners
    if (tier === "empty" || recs.length === 0) {
      root.appendChild(this._banner(
        "No recommendations yet — not enough similar products in the catalog.",
        "muted",
      ));
      return root;
    }
    if (tier === "partial") {
      root.appendChild(this._banner(
        `Limited matches — only ${recs.length} similar products available.`,
        "info",
      ));
    } else if (tier === "tier_2" && !this.props.demoMode) {
      root.appendChild(this._banner(
        "Showing broader matches: not enough exact-cohort peers in catalog.",
        "info",
      ));
    }

    // Header
    root.appendChild(this.$el("h3", {
      cls: "rec-grid-heading",
      text: `${recs.length} recommendation${recs.length === 1 ? "" : "s"}`,
    }));

    // Grid
    const grid = this.$el("div", { cls: "rec-grid" });
    for (const r of recs) {
      const cellHost = this.$el("div", { cls: "rec-grid-cell" });
      grid.appendChild(cellHost);
      new RecommendationCard(cellHost, {
        rec: r,
        debug: !!this.props.debug,
      }).mount();
    }
    root.appendChild(grid);
    return root;
  }

  _banner(text, tone) {
    return this.$el("div", { cls: `banner banner-${tone}`, text });
  }
}
