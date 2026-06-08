// Tiny base class shared by every component in the explorer.
//
// Contract (deliberately minimal so future surfaces can reuse without
// pulling in a framework):
//
//   constructor(parentEl, props = {})
//   mount()                  -> renders into parentEl, returns this
//   update(nextProps)        -> re-renders with merged props
//   destroy()                -> removes own DOM, drops handlers
//   on(event, handler)       -> subscribe to component-emitted events
//   emit(event, payload)     -> internal: emit a custom event
//
// Components don't share state. They communicate by emitting events
// upward; the page composition wires children together.
export class Component {
  constructor(parentEl, props = {}) {
    if (!parentEl) {
      throw new Error("Component requires a parent element");
    }
    this.parentEl = parentEl;
    this.props = props;
    this.el = null;
    this._listeners = new Map(); // event -> Set<handler>
  }

  mount() {
    this.el = this.render();
    if (this.el) this.parentEl.appendChild(this.el);
    return this;
  }

  update(nextProps = {}) {
    this.props = { ...this.props, ...nextProps };
    const newEl = this.render();
    if (this.el && newEl) {
      this.parentEl.replaceChild(newEl, this.el);
      this.el = newEl;
    } else if (newEl) {
      this.parentEl.appendChild(newEl);
      this.el = newEl;
    }
    return this;
  }

  destroy() {
    if (this.el && this.el.parentNode) {
      this.el.parentNode.removeChild(this.el);
    }
    this.el = null;
    this._listeners.clear();
  }

  on(event, handler) {
    if (!this._listeners.has(event)) this._listeners.set(event, new Set());
    this._listeners.get(event).add(handler);
    return this;
  }

  emit(event, payload) {
    const set = this._listeners.get(event);
    if (!set) return;
    for (const h of set) {
      try { h(payload); } catch (e) { console.error(e); }
    }
  }

  // Subclass MUST override.
  render() {
    throw new Error("Component subclass must implement render()");
  }

  // Helper: create an element with classes and text.
  $el(tag, opts = {}) {
    const e = document.createElement(tag);
    if (opts.cls) e.className = opts.cls;
    if (opts.text !== undefined) e.textContent = String(opts.text);
    if (opts.html !== undefined) e.innerHTML = opts.html;
    if (opts.attrs) {
      for (const [k, v] of Object.entries(opts.attrs)) e.setAttribute(k, v);
    }
    if (opts.children) {
      for (const c of opts.children) if (c) e.appendChild(c);
    }
    return e;
  }
}
