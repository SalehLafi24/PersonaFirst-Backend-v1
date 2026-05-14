// API wrapper. Single place for all backend calls.
// Reusable: any future page (CMS preview, storefront widget) can import
// this module without modification.
//
// Every fetch returns the parsed JSON body or throws an Error with a
// human-readable message. Network errors and 4xx/5xx surface uniformly.

const _BASE = ""; // same-origin

async function _fetchJson(path, params) {
  const url = new URL(_BASE + path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === null || v === undefined || v === "") continue;
      url.searchParams.set(k, String(v));
    }
  }
  const t0 = performance.now();
  const res = await fetch(url.toString(), { headers: { Accept: "application/json" } });
  const elapsedMs = performance.now() - t0;
  if (!res.ok) {
    let body = "";
    try { body = await res.text(); } catch { /* ignore */ }
    const err = new Error(`HTTP ${res.status} on ${path}: ${body.slice(0, 200)}`);
    err.status = res.status;
    err.elapsedMs = elapsedMs;
    throw err;
  }
  const data = await res.json();
  return { data, elapsedMs };
}

export const Api = {
  async listWorkspaces() {
    const { data } = await _fetchJson("/admin/taxonomy/api/workspaces");
    return data;
  },

  async listAnchors({ workspaceId, search, productType, limit = 50 }) {
    const { data, elapsedMs } = await _fetchJson("/admin/recommendations/anchors", {
      workspace_id: workspaceId,
      search,
      product_type: productType,
      limit,
    });
    return { anchors: data, elapsedMs };
  },

  async listCuratedAnchors({ workspaceId }) {
    const { data, elapsedMs } = await _fetchJson(
      "/admin/recommendations/curated_anchors",
      { workspace_id: workspaceId },
    );
    return { anchors: data, elapsedMs };
  },

  async exploreProduct({ workspaceId, productId, topN = 5, debug = false }) {
    const { data, elapsedMs } = await _fetchJson(
      `/admin/recommendations/explore/${encodeURIComponent(productId)}`,
      { workspace_id: workspaceId, top_n: topN, debug: debug ? "true" : "false" },
    );
    return { result: data, elapsedMs };
  },

  async listCustomers({ workspaceId }) {
    const { data, elapsedMs } = await _fetchJson(
      "/admin/recommendations/customers",
      { workspace_id: workspaceId },
    );
    return { customers: data, elapsedMs };
  },

  async exploreCustomer({ workspaceId, customerId, topN = 10 }) {
    const { data, elapsedMs } = await _fetchJson(
      `/admin/recommendations/customers/${encodeURIComponent(customerId)}/explore`,
      { workspace_id: workspaceId, top_n: topN },
    );
    return { result: data, elapsedMs };
  },
};
