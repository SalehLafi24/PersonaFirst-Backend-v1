// ProductPicker
//
// Props:
//   workspaceId         number
//   demoMode            bool        — when true, only curated anchors
//   curatedAnchors      array       — provided by parent in demo mode
//   onSelect(product)   handler     — emitted when a product is chosen
//   api                 {Api}       — injected API client
//
// Emits: "select" with the selected product object.
//
// Reusable: any future page that needs an anchor picker can drop this
// in. Demo mode makes it a list; non-demo mode makes it a typeahead.

import { Component } from "./Component.js";

export class ProductPicker extends Component {
  constructor(parentEl, props) {
    super(parentEl, props);
    this._timer = null;
    this._lastSearch = "";
    this._results = [];
  }

  render() {
    const root = this.$el("div", { cls: "picker" });

    if (this.props.demoMode) {
      // Demo: render the curated list as clickable buttons.
      root.appendChild(this.$el("div", {
        cls: "picker-label",
        text: "Pick a curated demo product:",
      }));
      const list = this.$el("div", { cls: "picker-curated-list" });
      const anchors = this.props.curatedAnchors || [];
      if (anchors.length === 0) {
        list.appendChild(this.$el("div", {
          cls: "picker-empty",
          text: "No curated anchors configured for this workspace.",
        }));
      } else {
        for (const a of anchors) {
          list.appendChild(this._renderResult(a, true));
        }
      }
      root.appendChild(list);
      return root;
    }

    // Non-demo: search typeahead.
    root.appendChild(this.$el("div", {
      cls: "picker-label",
      text: "Search by product name or SKU:",
    }));
    const input = this.$el("input", {
      cls: "picker-input",
      attrs: { type: "text", placeholder: "Type at least 2 characters..." },
    });
    input.addEventListener("input", (e) => this._onInput(e.target.value));
    root.appendChild(input);

    const results = this.$el("div", { cls: "picker-results" });
    if (this._results.length === 0 && this._lastSearch.length >= 2) {
      results.appendChild(this.$el("div", {
        cls: "picker-empty",
        text: "No matching products.",
      }));
    } else {
      for (const a of this._results) results.appendChild(this._renderResult(a, false));
    }
    root.appendChild(results);
    return root;
  }

  _renderResult(a, isCurated) {
    const card = this.$el("button", {
      cls: "picker-row" + (isCurated ? " picker-row-curated" : ""),
      attrs: { type: "button" },
    });
    const name = this.$el("div", { cls: "picker-row-name", text: a.name || a.product_id });
    const meta = this.$el("div", { cls: "picker-row-meta" });
    const tags = [a.product_type, a.age_group, a.gender].filter(Boolean);
    meta.textContent = tags.length ? tags.join(" · ") : "—";
    if (a.has_strong_recs) {
      const tag = this.$el("span", { cls: "picker-row-strong", text: "Strong" });
      meta.appendChild(tag);
    }
    card.appendChild(name);
    card.appendChild(meta);
    card.addEventListener("click", () => this.emit("select", a));
    return card;
  }

  _onInput(query) {
    this._lastSearch = query;
    if (this._timer) clearTimeout(this._timer);
    if (query.length < 2) {
      this._results = [];
      this.update();
      return;
    }
    this._timer = setTimeout(() => this._search(query), 220);
  }

  async _search(query) {
    try {
      const { anchors } = await this.props.api.listAnchors({
        workspaceId: this.props.workspaceId,
        search: query,
        limit: 25,
      });
      // Only update if the search hasn't changed under us.
      if (this._lastSearch === query) {
        this._results = anchors;
        this.update();
      }
    } catch (e) {
      console.warn("picker search failed", e);
    }
  }
}
