// AnchorProductCard — displays the selected anchor product.
//
// Props:
//   anchor       { product_id, name, image_url, attributes,
//                  coverage_missing, is_anomalous, anomaly_notes }
//   tier_label_human
//   tier_label_raw
//   debug        bool
//
// Reusable: same component would render the "subject product" in a CMS
// preview or storefront widget. The shape `anchor` is the integration
// contract.

import { Component } from "./Component.js";
import { anchorAttributesCaption } from "../explanation.js";

export class AnchorProductCard extends Component {
  render() {
    const root = this.$el("section", { cls: "anchor-card" });

    if (!this.props.anchor) {
      root.appendChild(this.$el("div", {
        cls: "anchor-empty",
        text: "Pick a product to begin.",
      }));
      return root;
    }

    const a = this.props.anchor;

    // Image / placeholder
    const img = this.$el("div", { cls: "anchor-image" });
    if (a.image_url) {
      const i = this.$el("img", { attrs: { src: a.image_url, alt: a.name || "" } });
      img.appendChild(i);
    } else {
      // Initial-letter placeholder
      const letter = (a.name || a.product_id || "?").trim()[0] || "?";
      img.appendChild(this.$el("div", {
        cls: "anchor-image-placeholder",
        text: letter.toUpperCase(),
      }));
    }
    root.appendChild(img);

    // Body
    const body = this.$el("div", { cls: "anchor-body" });
    body.appendChild(this.$el("h2", { cls: "anchor-name", text: a.name || a.product_id }));

    const caption = anchorAttributesCaption(a.attributes) || "—";
    body.appendChild(this.$el("div", { cls: "anchor-caption", text: caption }));

    // Tier label
    const tierEl = this.$el("div", { cls: "anchor-tier" });
    tierEl.appendChild(this.$el("span", {
      cls: "anchor-tier-label",
      text: this.props.tier_label_human || "—",
    }));
    if (this.props.debug && this.props.tier_label_raw) {
      tierEl.appendChild(this.$el("span", {
        cls: "anchor-tier-raw",
        text: ` (${this.props.tier_label_raw})`,
      }));
    }
    body.appendChild(tierEl);

    // Coverage indicator
    if (a.coverage_missing && a.coverage_missing.length > 0) {
      body.appendChild(this.$el("div", {
        cls: "anchor-coverage",
        text: `Missing: ${a.coverage_missing.join(", ")} — recommendations may be wider than ideal.`,
      }));
    }

    // Anomaly warning
    if (a.is_anomalous) {
      const w = this.$el("div", { cls: "anchor-anomaly" });
      w.appendChild(this.$el("strong", { text: "Possible attribute issue: " }));
      w.appendChild(document.createTextNode((a.anomaly_notes || []).join("; ")));
      body.appendChild(w);
    }
    root.appendChild(body);
    return root;
  }
}
