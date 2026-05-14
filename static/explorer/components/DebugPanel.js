// DebugPanel — collapsible run-level debug info shown only when debug
// mode is on AND demo mode is off.
//
// Props:
//   payload    full /admin/recommendations/explore/{id} response
//   elapsedMs  client-measured request latency
//
// Reusable: any future surface can mount this (typically only in admin
// contexts) to inspect the engine's full response.

import { Component } from "./Component.js";

export class DebugPanel extends Component {
  render() {
    const root = this.$el("section", { cls: "debug-panel" });
    if (!this.props.payload) return root;

    const head = this.$el("div", { cls: "debug-panel-head" });
    head.appendChild(this.$el("strong", { text: "Debug" }));
    const lat = this.props.elapsedMs;
    if (typeof lat === "number") {
      head.appendChild(this.$el("span", {
        cls: "debug-latency",
        text: ` ${Math.round(lat)} ms`,
      }));
    }
    root.appendChild(head);

    const r = this.props.payload;
    const lines = [
      `tier (raw)            : ${r.tier_label_raw}`,
      `tier (label)          : ${r.tier_label_human}`,
      `cohort_size_observed  : ${r.cohort_size_observed}`,
      `escalated_from        : ${r.escalated_from || "(none)"}`,
      `request_id            : ${r.request_id}`,
      `recommendations.length: ${(r.recommendations || []).length}`,
    ];
    root.appendChild(this.$el("pre", {
      cls: "debug-pre",
      text: lines.join("\n"),
    }));
    return root;
  }
}
