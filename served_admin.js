
// Immediate beacon: if you see "[js started]" in the status bar but the
// workspace dropdown stays at "(loading...)", the script is running but
// something failed during loadWorkspaces -- check the red banner / console.
// If you DON'T see "[js started]" at all, the script isn't being parsed
// (browser blocked it, stale cache, or uvicorn not serving the new code).
try {
  const _s = document.getElementById("status");
  if (_s) _s.textContent = "[js started] @ " + new Date().toISOString();
} catch (e) {}

const PAGE = 50;
let aggOffset = 0, prodOffset = 0;

function showError(msg) {
  const el = document.getElementById("errbar");
  el.textContent = "[error] " + msg;
  el.classList.add("visible");
  console.error(msg);
}
function clearError() {
  const el = document.getElementById("errbar");
  el.textContent = "";
  el.classList.remove("visible");
}
function setStatus(msg) {
  document.getElementById("status").textContent = msg || "";
}

// Surface any uncaught JS errors visibly so they don't fail silently.
window.addEventListener("error", e => showError("JS: " + (e.message || e)));
window.addEventListener("unhandledrejection",
  e => showError("Promise: " + (e.reason && e.reason.message || e.reason || "unknown")));

async function api(path, params = {}) {
  const url = new URL("/admin/taxonomy/api/" + path, window.location.origin);
  for (const k in params) if (params[k] !== "" && params[k] != null) url.searchParams.set(k, params[k]);
  let r;
  try {
    r = await fetch(url);
  } catch (e) {
    throw new Error("network error fetching " + url.pathname + url.search + ": " + e.message);
  }
  if (!r.ok) {
    let body = "";
    try { body = await r.text(); } catch {}
    throw new Error("HTTP " + r.status + " " + url.pathname + url.search + " -- " + body.slice(0, 200));
  }
  return r.json();
}

function ws()   { return document.getElementById("ws").value; }
function attr() { return document.getElementById("attr").value; }

async function loadWorkspaces() {
  setStatus("loading workspaces...");
  try {
    const list = await api("workspaces");
    const sel = document.getElementById("ws");
    sel.innerHTML = "";
    if (!list.length) {
      sel.innerHTML = '<option value="">(no workspaces)</option>';
      showError("workspaces endpoint returned an empty list");
      return;
    }
    for (const w of list) {
      const opt = document.createElement("option");
      opt.value = w.id;
      opt.textContent = `${w.slug} (id=${w.id})`;
      sel.appendChild(opt);
    }
    // Default to mumzworld_v3_sample if present, else first.
    const v3 = list.find(w => w.slug === "mumzworld_v3_sample");
    sel.value = (v3 || list[0]).id;
    setStatus(`${list.length} workspaces loaded`);
    clearError();
    refreshAll();
  } catch (e) {
    showError("loadWorkspaces failed: " + e.message);
    setStatus("");
  }
}

async function loadAggregates() {
  if (!ws()) return;
  let data;
  try {
    data = await api("aggregates", {
      workspace_id: ws(), attribute: attr(),
      status: document.getElementById("agg-status").value,
      search: document.getElementById("agg-search").value,
      recommended_only: document.getElementById("agg-recommended-only").checked ? "true" : "",
      offset: aggOffset, limit: PAGE,
    });
  } catch (e) { showError("aggregates: " + e.message); return; }
  const tbody = document.querySelector("#agg-table tbody");
  tbody.innerHTML = "";
  for (const a of data.items) {
    const tr = document.createElement("tr");
    const evid = (a.sample_evidence || []).map(e => `* ${e}`).join("<br>");
    const samp = (a.sample_product_ids || []).slice(0,3).join(", ");
    const readyPill = a.ready ? '<span class="pill ready">ready</span>' : '';
    const recPill = a.recommended_for_approval ? '<span class="pill recommended">recommended</span>' : '';
    const isPending = a.status === "pending";
    const approveDisabled = !(isPending && a.ready);
    const rejectDisabled  = !isPending;
    const approveBtn = `<button class="act act-approve" data-id="${a.id}"
        data-key="${a.cluster_key}" data-count="${a.count}"
        data-dist="${a.distinct_products}" data-conf="${a.avg_conf}"
        ${approveDisabled ? "disabled" : ""}>Approve</button>`;
    const rejectBtn = `<button class="act act-reject" data-id="${a.id}"
        data-key="${a.cluster_key}" data-count="${a.count}"
        data-dist="${a.distinct_products}" data-conf="${a.avg_conf}"
        ${rejectDisabled ? "disabled" : ""}>Reject</button>`;
    tr.innerHTML = `
      <td class=code>${a.cluster_key}</td>
      <td class=code>${a.canonical_value}</td>
      <td class=num>${a.count}</td>
      <td class=num>${a.distinct_products}</td>
      <td class=num>${a.avg_conf.toFixed(3)}</td>
      <td><span class="pill ${a.status}">${a.status}</span></td>
      <td class=code>${a.promoted_to_allowed_value || ""}</td>
      <td>${readyPill}</td>
      <td>${recPill}</td>
      <td><small class=evid>${evid}</small></td>
      <td><small class=dim>${samp}</small></td>
      <td class=actions>${approveBtn} ${rejectBtn}</td>`;
    tbody.appendChild(tr);
  }
  // Wire up newly-rendered action buttons.
  tbody.querySelectorAll(".act-approve").forEach(b =>
    b.addEventListener("click", () => onApprove(b.dataset)));
  tbody.querySelectorAll(".act-reject").forEach(b =>
    b.addEventListener("click", () => onReject(b.dataset)));
  document.getElementById("agg-count").textContent =
    `${data.total} aggregates (thresholds: count>=${data.thresholds.min_count}, ` +
    `distinct>=${data.thresholds.min_distinct}, avg_conf>=${data.thresholds.min_avg_conf})`;
  document.getElementById("agg-pageinfo").textContent =
    `${aggOffset + 1}-${Math.min(aggOffset + PAGE, data.total)} of ${data.total}`;
}
function aggPage(dir) {
  aggOffset = Math.max(0, aggOffset + dir * PAGE);
  loadAggregates();
}

async function postAction(verb, ds) {
  // Browser confirm with the cluster details -- required by the spec.
  const lines = [
    `${verb.toUpperCase()} aggregate?`,
    `cluster_key      : ${ds.key}`,
    `count            : ${ds.count}`,
    `distinct_products: ${ds.dist}`,
    `avg_conf         : ${(+ds.conf).toFixed(3)}`,
    `workspace        : id=${ws()}`,
    `attribute        : ${attr()}`,
  ];
  if (!confirm(lines.join("\n"))) return;
  const url = new URL(
    `/admin/taxonomy/api/aggregates/${ds.id}/${verb}`,
    window.location.origin
  );
  url.searchParams.set("workspace_id", ws());
  url.searchParams.set("attribute", attr());
  const r = await fetch(url, { method: "POST" });
  if (!r.ok) {
    const body = await r.text();
    alert(`Action failed (${r.status}): ${body}`);
    return;
  }
  // Refresh ALL four tabs so the user sees the consequence everywhere.
  refreshAll();
}
function onApprove(ds) { postAction("approve", ds); }
function onReject(ds)  { postAction("reject",  ds); }

async function loadAllowed() {
  if (!ws()) return;
  let data;
  try {
    data = await api("allowed_values", {
      workspace_id: ws(), attribute: attr(),
    });
  } catch (e) { showError("allowed_values: " + e.message); return; }
  const tbody = document.querySelector("#allowed-table tbody");
  tbody.innerHTML = "";
  for (const r of data) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class=code>${r.attribute_name}</td>
      <td class=code>${r.value}</td>
      <td>${r.is_active}</td>
      <td><small>${r.created_at || ""}</small></td>`;
    tbody.appendChild(tr);
  }
}

async function loadProducts() {
  if (!ws()) return;
  let data;
  try {
    data = await api("products", {
      workspace_id: ws(), attribute: attr(),
      status: document.getElementById("prod-status").value,
      search: document.getElementById("prod-search").value,
      offset: prodOffset, limit: PAGE,
    });
  } catch (e) { showError("products: " + e.message); return; }
  const tbody = document.querySelector("#prod-table tbody");
  tbody.innerHTML = "";
  for (const p of data.items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class=code>${p.product_id}</td>
      <td>${(p.name || "").slice(0,80)}</td>
      <td class=code>${p.assigned_value || ""}</td>
      <td class=code>${p.proposed_value || ""}</td>
      <td class=num>${p.proposed_confidence != null ? p.proposed_confidence.toFixed(2) : ""}</td>
      <td><span class="pill ${p.status}">${p.status}</span></td>`;
    tbody.appendChild(tr);
  }
  document.getElementById("prod-count").textContent = `${data.total} products`;
  document.getElementById("prod-pageinfo").textContent =
    `${prodOffset + 1}-${Math.min(prodOffset + PAGE, data.total)} of ${data.total}`;
}
function prodPage(dir) {
  prodOffset = Math.max(0, prodOffset + dir * PAGE);
  loadProducts();
}

async function loadCoverage() {
  if (!ws()) return;
  let c;
  try {
    c = await api("coverage", {
      workspace_id: ws(), attribute: attr(),
    });
  } catch (e) { showError("coverage: " + e.message); return; }
  const el = document.getElementById("cov-metrics");
  const fmt = (label, val) =>
    `<div class=metric><div class=label>${label}</div><div class=value>${val}</div></div>`;
  el.innerHTML =
    fmt("total products", c.total_products) +
    fmt("with " + c.attribute, c.products_with_attribute) +
    fmt("coverage %", c.coverage_pct + "%") +
    fmt("approved values", c.approved_value_count) +
    fmt("pending aggregates", c.pending_aggregate_count) +
    fmt("long tail (count==1)", c.long_tail_count) +
    fmt("missing", c.missing_count);
  document.getElementById("ctxinfo").textContent =
    `${c.total_products} products | ${c.products_with_attribute} assigned | ` +
    `${c.coverage_pct}% coverage`;
}

async function loadMerges() {
  if (!ws()) return;
  let data;
  try {
    data = await api("merge_suggestions", { workspace_id: ws(), attribute: attr() });
  } catch (e) { showError("merge_suggestions: " + e.message); return; }
  const tbody = document.querySelector("#merges-table tbody");
  tbody.innerHTML = "";
  for (const s of data.items) {
    const tr = document.createElement("tr");
    const sevid = (s.sample_source_evidence || []).slice(0,2).map(x => "* " + x).join("<br>");
    const tevid = (s.sample_target_evidence || []).slice(0,2).map(x => "* " + x).join("<br>");
    const risks = (s.risk_notes || []).map(r => "&#9888; " + r).join("<br>");
    const disabled = s.executable ? "" : "disabled";
    const btn = `<button class="act act-merge" data-source="${s.source_cluster}"
        data-target="${s.target_cluster}" data-type="${s.merge_type}"
        data-srcct="${s.source_count}" data-tgtct="${s.target_count}"
        data-srcst="${s.source_status}" data-tgtst="${s.target_status}"
        ${disabled}>Approve Merge</button>`;
    tr.innerHTML = `
      <td class=code>${s.source_cluster} <small class=dim>(${s.source_status})</small></td>
      <td class=code>${s.target_cluster} <small class=dim>(${s.target_status})</small></td>
      <td><span class="pill ${s.merge_type}">${s.merge_type.replace("_"," ")}</span></td>
      <td><span class="pill ${s.confidence}">${s.confidence}</span></td>
      <td class=num>${s.source_count}</td>
      <td class=num>${s.target_count}</td>
      <td class=num>${s.combined_count}</td>
      <td><small>${s.reason}</small></td>
      <td><small class=evid><b>src:</b><br>${sevid}<br><b>tgt:</b><br>${tevid}</small></td>
      <td><small class=evid>${risks}</small></td>
      <td class=actions>${btn}</td>`;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll(".act-merge").forEach(b =>
    b.addEventListener("click", () => onApproveMerge(b.dataset)));
  document.getElementById("merges-count").textContent =
    `${data.total} suggestions  (norm=${data.by_type_count.normalization_variant}, ` +
    `parent_child=${data.by_type_count.parent_child}, ` +
    `semantic=${data.by_type_count.semantic_duplicate})`;
}

async function onApproveMerge(ds) {
  const lines = [
    "MERGE aggregate?",
    `source           : ${ds.source} (status=${ds.srcst}, count=${ds.srcct})`,
    `target           : ${ds.target} (status=${ds.tgtst}, count=${ds.tgtct})`,
    `merge_type       : ${ds.type}`,
    "",
    "ProductAttribute rows on the source value will be updated to point to",
    "the target. Per-product duplicates will be dropped (one product_type",
    "per product). Underlying ProposedAttributeValueEvent rows are preserved.",
  ];
  if (!confirm(lines.join("\n"))) return;
  const url = new URL(
    "/admin/taxonomy/api/merge_suggestions/execute",
    window.location.origin
  );
  const body = {
    workspace_id: parseInt(ws(), 10),
    attribute: attr(),
    source_cluster: ds.source,
    target_cluster: ds.target,
    merge_type: ds.type,
    review_note: "Merged from taxonomy admin UI",
  };
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text();
    alert(`Merge failed (${r.status}): ${text}`);
    return;
  }
  refreshAll();
}

function refreshAll() {
  aggOffset = 0; prodOffset = 0;
  loadAggregates(); loadAllowed(); loadProducts(); loadCoverage(); loadMerges();
}

document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  document.getElementById(t.dataset.tab).classList.add("active");
}));

document.getElementById("ws").addEventListener("change", refreshAll);
document.getElementById("attr").addEventListener("change", refreshAll);

loadWorkspaces();
