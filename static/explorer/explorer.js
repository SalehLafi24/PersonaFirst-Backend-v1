// explorer.js — page composition. Two modes: Product and Customer.
//
// Reusable components do their own rendering; this file owns page-level
// state (workspace, mode, demo/debug toggles, current selection).

import { Api } from "./api.js";
import {
  DemoConfig,
  EXPLORER_MODE_PRODUCT,
  EXPLORER_MODE_CUSTOMER,
} from "./demo_config.js";

import { ProductPicker } from "./components/ProductPicker.js";
import { CustomerPicker } from "./components/CustomerPicker.js";
import { AnchorProductCard } from "./components/AnchorProductCard.js";
import { CustomerPersonaCard } from "./components/CustomerPersonaCard.js";
import { RecommendationGrid } from "./components/RecommendationGrid.js";
import { DebugPanel } from "./components/DebugPanel.js";

const els = {
  wsSelect:    document.getElementById("ws-select"),
  demoToggle:  document.getElementById("demo-toggle"),
  debugToggle: document.getElementById("debug-toggle"),
  modeProduct: document.getElementById("mode-product"),
  modeCustomer:document.getElementById("mode-customer"),
  pickerHost:  document.getElementById("picker-host"),
  anchorHost:  document.getElementById("anchor-host"),
  gridHost:    document.getElementById("grid-host"),
  debugHost:   document.getElementById("debug-host"),
  statusBar:   document.getElementById("status-bar"),
};

const state = {
  mode: EXPLORER_MODE_PRODUCT,    // EXPLORER_MODE_PRODUCT | EXPLORER_MODE_CUSTOMER
  workspaceId: null,
  workspaces: [],
  curatedAnchors: [],
  picker: null,
  anchorCard: null,
  grid: null,
  debugPanel: null,
  result: null,                    // product-mode result OR customer-mode result
  resultElapsedMs: null,
};

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

(async function init() {
  state.mode = DemoConfig.getMode();
  els.demoToggle.checked = DemoConfig.isDemoMode();
  els.debugToggle.checked = DemoConfig.isDebugMode();
  applyModeVisuals();
  applyDemoVisuals();

  els.demoToggle.addEventListener("change", onDemoToggle);
  els.debugToggle.addEventListener("change", onDebugToggle);
  els.modeProduct.addEventListener("click", () => switchMode(EXPLORER_MODE_PRODUCT));
  els.modeCustomer.addEventListener("click", () => switchMode(EXPLORER_MODE_CUSTOMER));

  try {
    state.workspaces = await Api.listWorkspaces();
  } catch (e) {
    showStatus(`Failed to load workspaces: ${e.message}`);
    return;
  }
  populateWorkspaceSelect();

  const stored = DemoConfig.getWorkspaceId();
  const initialWs = state.workspaces.find((w) => w.id === stored)
                 || state.workspaces.find((w) => w.slug === "mumzworld_v3_sample")
                 || state.workspaces[0];
  if (initialWs) {
    els.wsSelect.value = String(initialWs.id);
    await switchWorkspace(initialWs.id);
  }
  els.wsSelect.addEventListener("change", (e) => switchWorkspace(Number(e.target.value)));
})();

// ---------------------------------------------------------------------------
// Workspace / mode handlers
// ---------------------------------------------------------------------------

function populateWorkspaceSelect() {
  els.wsSelect.innerHTML = "";
  for (const w of state.workspaces) {
    const opt = document.createElement("option");
    opt.value = String(w.id); opt.textContent = `${w.slug}  (id ${w.id})`;
    els.wsSelect.appendChild(opt);
  }
}

async function switchWorkspace(workspaceId) {
  state.workspaceId = workspaceId;
  DemoConfig.setWorkspaceId(workspaceId);
  if (state.mode === EXPLORER_MODE_PRODUCT) {
    try {
      const { anchors } = await Api.listCuratedAnchors({ workspaceId });
      state.curatedAnchors = anchors;
    } catch (e) { state.curatedAnchors = []; }
  }
  state.result = null; state.resultElapsedMs = null;
  rebuildPicker();
  renderResults();
}

function switchMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  DemoConfig.setMode(mode);
  applyModeVisuals();
  applyDemoVisuals();
  state.result = null; state.resultElapsedMs = null;
  if (mode === EXPLORER_MODE_PRODUCT) {
    // Re-fetch curated anchors when entering product mode
    Api.listCuratedAnchors({ workspaceId: state.workspaceId })
      .then(({ anchors }) => { state.curatedAnchors = anchors; rebuildPicker(); })
      .catch(() => { state.curatedAnchors = []; rebuildPicker(); });
  }
  rebuildPicker();
  renderResults();
}

function applyModeVisuals() {
  els.modeProduct.classList.toggle("mode-active", state.mode === EXPLORER_MODE_PRODUCT);
  els.modeCustomer.classList.toggle("mode-active", state.mode === EXPLORER_MODE_CUSTOMER);
  document.body.classList.toggle("mode-customer", state.mode === EXPLORER_MODE_CUSTOMER);
  document.body.classList.toggle("mode-product", state.mode === EXPLORER_MODE_PRODUCT);
}

function onDemoToggle(e) {
  DemoConfig.setDemoMode(e.target.checked);
  applyDemoVisuals();
  if (state.mode === EXPLORER_MODE_PRODUCT) {
    rebuildPicker();
    if (state.result && state.result.anchor) {
      fetchProductRecsFor(state.result.anchor.product_id);
    }
  }
}

function onDebugToggle(e) {
  DemoConfig.setDebugMode(e.target.checked);
  if (state.result) {
    if (state.mode === EXPLORER_MODE_PRODUCT && state.result.anchor) {
      fetchProductRecsFor(state.result.anchor.product_id);
    } else if (state.mode === EXPLORER_MODE_CUSTOMER && state.result.customer_id) {
      fetchCustomerRecsFor(state.result.customer_id);
    } else {
      renderResults();
    }
  } else { renderResults(); }
}

function applyDemoVisuals() {
  // Demo Mode is only meaningful in Product Mode (curated anchors).
  // In Customer Mode we hide the demo toggle via CSS but also clear it.
  const productMode = state.mode === EXPLORER_MODE_PRODUCT;
  const demoOn = productMode && DemoConfig.isDemoMode();
  document.body.classList.toggle("demo-on", demoOn);
  if (demoOn) {
    els.debugToggle.checked = false;
    DemoConfig.setDebugMode(false);
  }
}

// ---------------------------------------------------------------------------
// Picker building
// ---------------------------------------------------------------------------

function rebuildPicker() {
  if (state.picker) state.picker.destroy();
  if (state.mode === EXPLORER_MODE_PRODUCT) {
    state.picker = new ProductPicker(els.pickerHost, {
      workspaceId: state.workspaceId,
      demoMode: DemoConfig.isDemoMode(),
      curatedAnchors: state.curatedAnchors,
      api: Api,
    }).on("select", (p) => fetchProductRecsFor(p.product_id)).mount();
  } else {
    state.picker = new CustomerPicker(els.pickerHost, {
      workspaceId: state.workspaceId, api: Api,
    }).on("select", (c) => fetchCustomerRecsFor(c.customer_id)).mount();
  }
}

// ---------------------------------------------------------------------------
// Fetchers
// ---------------------------------------------------------------------------

async function fetchProductRecsFor(productId) {
  showStatus("Loading...");
  try {
    const { result, elapsedMs } = await Api.exploreProduct({
      workspaceId: state.workspaceId,
      productId, topN: 5,
      debug: DemoConfig.isDebugMode(),
    });
    state.result = { __mode: EXPLORER_MODE_PRODUCT, ...result };
    state.resultElapsedMs = elapsedMs;
    showStatus(`OK · ${Math.round(elapsedMs)} ms`, 1500);
    renderResults();
  } catch (e) { showStatus(`Failed: ${e.message}`); }
}

async function fetchCustomerRecsFor(customerId) {
  showStatus("Loading...");
  try {
    const { result, elapsedMs } = await Api.exploreCustomer({
      workspaceId: state.workspaceId, customerId, topN: 10,
    });
    state.result = { __mode: EXPLORER_MODE_CUSTOMER, ...result };
    state.resultElapsedMs = elapsedMs;
    showStatus(`OK · ${Math.round(elapsedMs)} ms`, 1500);
    renderResults();
  } catch (e) { showStatus(`Failed: ${e.message}`); }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderResults() {
  if (state.anchorCard) state.anchorCard.destroy();
  if (state.grid) state.grid.destroy();
  if (state.debugPanel) state.debugPanel.destroy();
  state.anchorCard = state.grid = state.debugPanel = null;

  if (state.mode === EXPLORER_MODE_PRODUCT) renderProductResults();
  else renderCustomerResults();
}

function renderProductResults() {
  const result = (state.result && state.result.__mode === EXPLORER_MODE_PRODUCT) ? state.result : null;
  state.anchorCard = new AnchorProductCard(els.anchorHost, {
    anchor: result ? result.anchor : null,
    tier_label_human: result ? result.tier_label_human : "",
    tier_label_raw:   result ? result.tier_label_raw   : "",
    debug: DemoConfig.isDebugMode(),
  }).mount();
  state.grid = new RecommendationGrid(els.gridHost, {
    tier:             result ? result.tier               : null,
    tier_label_human: result ? result.tier_label_human   : "",
    recommendations:  result ? result.recommendations    : [],
    debug:            DemoConfig.isDebugMode(),
    demoMode:         DemoConfig.isDemoMode(),
  }).mount();
  if (DemoConfig.isDebugMode() && !DemoConfig.isDemoMode() && result) {
    state.debugPanel = new DebugPanel(els.debugHost, {
      payload: result, elapsedMs: state.resultElapsedMs,
    }).mount();
  }
}

function renderCustomerResults() {
  const result = (state.result && state.result.__mode === EXPLORER_MODE_CUSTOMER) ? state.result : null;
  state.anchorCard = new CustomerPersonaCard(els.anchorHost, {
    persona: result ? result.persona : null,
    interactions: result ? result.interactions : [],
    intent_summary: result ? result.intent_summary : null,
    intent_dropped: result ? result.intent_dropped : [],
  }).mount();
  state.grid = new RecommendationGrid(els.gridHost, {
    tier:             null,
    tier_label_human: "",
    recommendations:  result ? result.recommendations : [],
    debug:            DemoConfig.isDebugMode(),
    demoMode:         false,
  }).mount();
  if (DemoConfig.isDebugMode() && result) {
    state.debugPanel = new DebugPanel(els.debugHost, {
      payload: {
        // Reuse DebugPanel fields by mapping customer-mode shape onto
        // the same keys it expects.
        tier_label_raw: result.persona.cold_start ? "cold_start" : "active",
        tier_label_human: `${(result.persona.confidence_overall * 100).toFixed(0)}% confidence`,
        cohort_size_observed: result.candidates_considered,
        escalated_from: null,
        request_id: result.computed_at,
        recommendations: result.recommendations,
      },
      elapsedMs: state.resultElapsedMs,
    }).mount();
  }
}

// ---------------------------------------------------------------------------
// Status bar
// ---------------------------------------------------------------------------

let _statusTimer = null;
function showStatus(msg, autohideMs) {
  els.statusBar.textContent = msg;
  els.statusBar.hidden = false;
  if (_statusTimer) clearTimeout(_statusTimer);
  if (autohideMs) {
    _statusTimer = setTimeout(() => { els.statusBar.hidden = true; }, autohideMs);
  }
}
