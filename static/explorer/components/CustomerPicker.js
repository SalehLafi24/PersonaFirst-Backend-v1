// CustomerPicker — list of customers in the workspace.
//
// Props:
//   workspaceId
//   api          {Api}
//   onSelect emitted as "select" with the chosen customer
//
// Customer Mode counterpart of ProductPicker. Same interaction model:
// emit a "select" event upward; the page wires it to a fetch.
//
// Reusable: a future campaign-preview surface that needs a customer picker
// can drop this in unchanged.

import { Component } from "./Component.js";

export class CustomerPicker extends Component {
  constructor(parentEl, props) {
    super(parentEl, props);
    this._customers = [];
    this._loaded = false;
    this._loading = false;
    this._loadCustomers();
  }

  async _loadCustomers() {
    if (this._loading) return;
    this._loading = true;
    try {
      const { customers } = await this.props.api.listCustomers({
        workspaceId: this.props.workspaceId,
      });
      this._customers = customers;
      this._loaded = true;
      this.update();
    } catch (e) {
      console.warn("customer list failed", e);
      this._loaded = true;
      this.update();
    } finally {
      this._loading = false;
    }
  }

  render() {
    const root = this.$el("div", { cls: "picker" });
    root.appendChild(this.$el("div", {
      cls: "picker-label",
      text: "Pick a customer:",
    }));
    if (!this._loaded) {
      root.appendChild(this.$el("div", {
        cls: "picker-empty",
        text: "Loading customers...",
      }));
      return root;
    }
    if (this._customers.length === 0) {
      root.appendChild(this.$el("div", {
        cls: "picker-empty",
        text: "No customers in this workspace yet. Run scripts/generate_synthetic_customers.py.",
      }));
      return root;
    }
    const list = this.$el("div", { cls: "picker-curated-list" });
    for (const c of this._customers) list.appendChild(this._renderRow(c));
    root.appendChild(list);
    return root;
  }

  _renderRow(c) {
    const card = this.$el("button", {
      cls: "picker-row",
      attrs: { type: "button" },
    });
    card.appendChild(this.$el("div", { cls: "picker-row-name", text: c.customer_id }));
    const meta = this.$el("div", { cls: "picker-row-meta" });
    const last = c.last_interaction_at ? new Date(c.last_interaction_at).toISOString().slice(0, 10) : "—";
    meta.textContent = `${c.interaction_count} interactions · last ${last}`;
    card.appendChild(meta);
    card.addEventListener("click", () => this.emit("select", c));
    return card;
  }
}
