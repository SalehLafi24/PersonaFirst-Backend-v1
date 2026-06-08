// Demo / debug mode configuration. Persisted to localStorage so the
// chosen mode survives page reloads. Centralised so any future surface
// (CMS preview, storefront widget) can read the same toggles.

const _DEMO_KEY = "pf_explorer_demo_mode";
const _DEBUG_KEY = "pf_explorer_debug_mode";
const _WS_KEY = "pf_explorer_workspace_id";
const _MODE_KEY = "pf_explorer_mode";   // "product" | "customer"

export const EXPLORER_MODE_PRODUCT = "product";
export const EXPLORER_MODE_CUSTOMER = "customer";

export const DemoConfig = {
  isDemoMode() {
    const v = localStorage.getItem(_DEMO_KEY);
    return v === null ? true : v === "1"; // default ON
  },
  setDemoMode(on) {
    localStorage.setItem(_DEMO_KEY, on ? "1" : "0");
  },

  isDebugMode() {
    return localStorage.getItem(_DEBUG_KEY) === "1";
  },
  setDebugMode(on) {
    localStorage.setItem(_DEBUG_KEY, on ? "1" : "0");
  },

  getWorkspaceId() {
    const v = localStorage.getItem(_WS_KEY);
    return v ? Number(v) : null;
  },
  setWorkspaceId(id) {
    if (id) localStorage.setItem(_WS_KEY, String(id));
    else localStorage.removeItem(_WS_KEY);
  },

  getMode() {
    const v = localStorage.getItem(_MODE_KEY);
    return v === EXPLORER_MODE_CUSTOMER ? EXPLORER_MODE_CUSTOMER : EXPLORER_MODE_PRODUCT;
  },
  setMode(mode) {
    localStorage.setItem(_MODE_KEY, mode);
  },
};

// Tier label mapping (mirror of backend humanize_tier). Demo mode uses
// the human form; non-demo shows the raw tier name from the API alongside.
export const TIER_LABELS = {
  tier_1: "Strong match",
  tier_2: "Broader match",
  partial: "Limited matches",
  empty: "No recommendations",
};

export function tierToHuman(tier) {
  return TIER_LABELS[tier] || tier;
}
