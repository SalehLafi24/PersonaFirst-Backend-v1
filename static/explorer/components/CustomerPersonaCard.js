// CustomerPersonaCard — anchor area for Customer Mode.
//
// Replaces AnchorProductCard when the Explorer is in customer mode.
// Shows the customer id, persona summary (top values per attribute,
// confidence and focus), and the recent interaction list (with the
// product attributes that drove the persona).
//
// Reusable: a future campaign-preview UI can drop this in to display
// a customer's persona without modification.

import { Component } from "./Component.js";

export class CustomerPersonaCard extends Component {
  render() {
    const root = this.$el("section", { cls: "persona-card" });
    if (!this.props.persona) {
      root.appendChild(this.$el("div", {
        cls: "anchor-empty",
        text: "Pick a customer to begin.",
      }));
      return root;
    }
    const p = this.props.persona;

    // Header
    const head = this.$el("div", { cls: "persona-head" });
    head.appendChild(this.$el("h2", {
      cls: "persona-name",
      text: p.customer_id,
    }));
    head.appendChild(this.$el("div", {
      cls: "persona-meta",
      text: `${p.interaction_count_total} interactions · `
            + `confidence ${(p.confidence_overall * 100).toFixed(0)}%`
            + (p.cold_start ? " · cold start" : ""),
    }));
    if (p.cold_start && p.cold_start_reasons && p.cold_start_reasons.length) {
      head.appendChild(this.$el("div", {
        cls: "persona-cold-start",
        text: `Cold-start reasons: ${p.cold_start_reasons.join("; ")}`,
      }));
    }
    root.appendChild(head);

    // Affinities table
    const grid = this.$el("div", { cls: "persona-affinities" });
    for (const attr of ["product_type", "age_group", "gender"]) {
      const a = p.attribute_affinities?.[attr];
      if (!a) continue;
      grid.appendChild(this._renderAffinityBlock(attr, a));
    }
    root.appendChild(grid);

    // Intent layer summary (Phase 1: RepurchaseSuppression)
    const intent = this.props.intent_summary;
    const dropped = this.props.intent_dropped || [];
    if (intent && (intent.dropped_count > 0 || (intent.signals_applied || []).length > 0)) {
      const block = this.$el("div", { cls: "persona-intent" });
      const head = this.$el("div", { cls: "persona-intent-head" });
      head.appendChild(this.$el("strong", { text: "Intent layer: " }));
      const sigs = intent.signals_applied || [];
      const sigText = sigs.length ? sigs.join(", ") : "(no signals applied)";
      head.appendChild(document.createTextNode(
        ` ${sigText} · ${intent.dropped_count} dropped of ${intent.dropped_count + intent.kept_count}`,
      ));
      block.appendChild(head);
      if (dropped.length) {
        const drops = this.$el("ul", { cls: "intent-drop-list" });
        for (const d of dropped.slice(0, 6)) {
          const li = this.$el("li", { cls: "intent-drop-row" });
          li.appendChild(this.$el("span", { cls: "intent-drop-name", text: d.name || d.product_id }));
          li.appendChild(this.$el("span", {
            cls: "intent-drop-reason",
            text: (d.reasons && d.reasons[0]) || "",
          }));
          drops.appendChild(li);
        }
        if (dropped.length > 6) {
          drops.appendChild(this.$el("li", {
            cls: "intent-drop-more",
            text: `+ ${dropped.length - 6} more...`,
          }));
        }
        block.appendChild(drops);
      }
      root.appendChild(block);
    }

    // Interaction list
    const interactions = this.props.interactions || [];
    if (interactions.length) {
      const inter = this.$el("div", { cls: "persona-interactions" });
      inter.appendChild(this.$el("h3", {
        cls: "persona-section-heading",
        text: `Interactions (${interactions.length})`,
      }));
      const list = this.$el("ul", { cls: "interaction-list" });
      for (const i of interactions) {
        const li = this.$el("li", { cls: "interaction-row" });
        const name = this.$el("span", {
          cls: "interaction-name",
          text: i.name,
        });
        const tag = (i.attributes && [i.attributes.product_type, i.attributes.age_group, i.attributes.gender]
          .filter(Boolean).join(" · ")) || "—";
        const meta = this.$el("span", { cls: "interaction-meta", text: tag });
        li.appendChild(name);
        li.appendChild(meta);
        list.appendChild(li);
      }
      inter.appendChild(list);
      root.appendChild(inter);
    }

    return root;
  }

  _renderAffinityBlock(attr, a) {
    const block = this.$el("div", { cls: "persona-affinity" });
    block.appendChild(this.$el("div", {
      cls: "persona-affinity-name",
      text: attr.replace("_", " "),
    }));
    const dist = a.distribution || {};
    const sorted = Object.entries(dist).sort((x, y) => y[1] - x[1]);
    if (sorted.length === 0) {
      block.appendChild(this.$el("div", {
        cls: "persona-affinity-empty",
        text: "(no data)",
      }));
    } else {
      const bars = this.$el("div", { cls: "persona-bars" });
      for (const [val, prob] of sorted.slice(0, 5)) {
        const row = this.$el("div", { cls: "persona-bar-row" });
        row.appendChild(this.$el("span", { cls: "persona-bar-label", text: val }));
        const barOuter = this.$el("div", { cls: "persona-bar-outer" });
        const barInner = this.$el("div", { cls: "persona-bar-inner" });
        barInner.style.width = `${(prob * 100).toFixed(0)}%`;
        barOuter.appendChild(barInner);
        row.appendChild(barOuter);
        row.appendChild(this.$el("span", {
          cls: "persona-bar-pct",
          text: `${(prob * 100).toFixed(0)}%`,
        }));
        bars.appendChild(row);
      }
      block.appendChild(bars);
    }
    const stats = this.$el("div", { cls: "persona-affinity-stats" });
    const parts = [
      `focus ${(a.focus_score * 100).toFixed(0)}%`,
      `coverage ${(a.coverage_pct * 100).toFixed(0)}%`,
      `conf ${(a.confidence * 100).toFixed(0)}%`,
    ];
    stats.textContent = parts.join(" · ");
    block.appendChild(stats);
    return block;
  }
}
