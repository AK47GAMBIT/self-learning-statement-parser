"use strict";

// Everything shown here comes from PDFs and emails, so it is untrusted text.
// el() only creates elements and text nodes; we never build HTML from strings.

const PAGE = 25;            // documents per page
const POLL_MS = 20000;      // auto-refresh interval
const STATUS_LABEL = {
  parsed_ok: "OK", needs_review: "Needs review", failed: "Failed",
  learning_failed: "Learning failed", password_required: "Password needed",
};
const state = { tab: "documents", filter: "attention", q: "", page: 0, selected: null, showNoPdf: false };
let token = sessionStorage.getItem("review_token") || "";
let renderSeq = 0;          // lets a slow, outdated response be ignored

class AuthError extends Error {}
const $ = (id) => document.getElementById(id);

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

// Display-only. Values arrive as exact decimal strings and STAY strings: we only regroup the
// digits (Indian lakh/crore style). Nothing is ever converted to a floating-point number.
function fmt(value, minDecimals = 2) {
  if (value === null || value === undefined || value === "") return "–";
  const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(value).trim());
  if (!m) return String(value);
  let frac = (m[3] || "").replace(/0+$/, "");
  while (frac.length < minDecimals) frac += "0";
  const tail = m[2].slice(-3);
  const head = m[2].slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  return m[1] + (head ? head + "," : "") + tail + (frac ? "." + frac : "");
}
const txt = (v) => (v === null || v === undefined || v === "") ? "–" : String(v);
const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

// ------------------------------------------------------------------ access token
function showLogin(message) {
  $("login-error").textContent = message || "";
  $("login").hidden = false;
  $("token").value = "";
  $("token").focus();
}

async function api(path, options) {
  const opts = options || {};
  const headers = { "X-Review-Token": token };
  if (opts.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { method: opts.method || "GET", headers, body: opts.body });
  if (response.status === 401) {
    showLogin(token ? "That token was not accepted." : "");
    throw new AuthError("unauthorised");
  }
  if (!response.ok) {
    let message = path + " returned " + response.status;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch (e) { /* keep the default message */ }
    throw new Error(message);
  }
  return response.json();
}

// ------------------------------------------------------------------ small building blocks
function badge(status) {
  const known = status in STATUS_LABEL;
  return el("span", { class: "badge " + (known ? status : "unknown") },
    known ? STATUS_LABEL[status] : status);
}

function reviewPill(review) {
  if (!review || (review.decision !== "approved" && review.decision !== "rejected")) return null;
  return el("span", { class: "pill " + review.decision },
    review.decision === "approved" ? "Approved by reviewer" : "Rejected by reviewer");
}

function table(headers, rows, numeric) {
  const num = new Set(numeric || []);
  return el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, headers.map((h, i) => el("th", { class: num.has(i) ? "num" : null }, h)))),
    el("tbody", {}, rows.map((r) => el("tr",
      { class: (r.onclick ? "click " : "") + (r.selected ? "selected" : ""), onclick: r.onclick },
      r.cells.map((c, i) => el("td", { class: num.has(i) ? "num" : null }, c)))))));
}

function kv(pairs) {
  return el("dl", { class: "kv" },
    pairs.filter(([, v]) => v !== null && v !== undefined && v !== "")
      .map(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
}

function card(label, value, onclick, accent) {
  return el("div", { class: "card " + (accent || "") + (onclick ? " click" : ""), onclick },
    el("div", { class: "big" }, value), el("div", { class: "label" }, label));
}

function go(tab, filter) {
  state.tab = tab;
  if (filter) state.filter = filter;
  state.page = 0;
  state.selected = null;
  render();
}

function filterBar(options) {
  return el("div", { class: "seg" }, options.map(([key, label]) =>
    el("button", {
      class: state.filter === key ? "active" : "",
      onclick: () => { state.filter = key; state.page = 0; state.selected = null; render(); },
    }, label)));
}

function renderPager(pager, data) {
  if (data.total === 0) { pager.replaceChildren(); return; }
  const from = data.offset + 1;
  const to = data.offset + data.items.length;
  pager.replaceChildren(
    el("span", { class: "muted" }, "Showing " + from + "–" + to + " of " + data.total),
    data.total > PAGE ? el("div", { class: "buttons" },
      el("button", { disabled: state.page === 0 ? "" : null,
                     onclick: () => { state.page -= 1; render(); } }, "Previous"),
      el("button", { disabled: to >= data.total ? "" : null,
                     onclick: () => { state.page += 1; render(); } }, "Next")) : null);
}

// ------------------------------------------------------------------ cards
async function renderCards() {
  const s = await api("/api/summary");
  $("cards").replaceChildren(
    card("Documents", s.total, () => go("documents", "all")),
    card("Need attention", s.needs_attention, () => go("documents", "attention"),
      s.needs_attention ? "warn" : "ok"),
    card("Parsed OK", s.documents.parsed_ok || 0, () => go("documents", "ok"), "ok"),
    card("Reviewed by a person", s.reviewed.approved + s.reviewed.rejected, null, "info"),
    card("Used the LLM", s.llm_documents, null),
    card("Known formats", s.formats, () => go("formats")));
}

// ------------------------------------------------------------------ document detail
function reviewBlock(d, resolved) {
  if (d.status === "parsed_ok") return null;
  const message = el("p", { class: "error" });
  const note = el("textarea", { rows: "2", maxlength: "1000", placeholder: "Optional note: what did you check?" });
  const send = async (decision) => {
    message.textContent = "";
    try {
      await api("/api/documents/" + d.id + "/review",
        { method: "POST", body: JSON.stringify({ decision, note: note.value }) });
      await render();
    } catch (e) {
      if (!(e instanceof AuthError)) message.textContent = e.message;
    }
  };
  const buttons = [];
  if (resolved) {
    buttons.push(el("button", { onclick: () => send("reopened") }, "Reopen"));
  } else {
    if (d.status === "needs_review") {
      buttons.push(el("button", { class: "primary", onclick: () => send("approved") }, "Approve data"));
    }
    buttons.push(el("button", { class: "danger", onclick: () => send("rejected") },
      d.status === "needs_review" ? "Reject" : "Dismiss"));
  }
  const history = d.review_history.map((h) =>
    el("li", {}, h.decision + " · " + h.reviewed_at + " UTC" + (h.note ? " — " + h.note : "")));
  return el("div", { class: "review" },
    el("h3", {}, "Human review"),
    el("p", { class: "hint" }, resolved
      ? "Resolved. The machine's status and data above are unchanged."
      : "Approving confirms the extracted data. It does not change the stored status or the learned config."),
    resolved ? null : note,
    el("div", { class: "row" }, buttons),
    message,
    history.length ? el("ul", { class: "issues sub" }, history) : null);
}

async function docDetail(id) {
  const d = await api("/api/documents/" + id);
  const resolved = !!d.review && (d.review.decision === "approved" || d.review.decision === "rejected");
  const st = d.statement;
  const parts = [
    el("div", { class: "drawer-head" },
      el("div", {}, el("h2", {}, d.filename),
        el("div", { class: "row" }, badge(d.status), reviewPill(d.review))),
      el("button", { class: "ghost", onclick: () => { state.selected = null; render(); } }, "Close")),
    kv([
      ["Provider", d.provider], ["Strategy", d.strategy_name], ["Client", d.client_id],
      ["Type", d.statement_type && d.statement_type.toUpperCase()],
      ["Period", d.period_start && (d.period_start + " → " + d.period_end)],
      ["Currency", d.currency],
      ["Format", d.format_name && (d.format_name + " (" + d.match_kind + ", score " + d.match_score + ")")],
      ["LLM used", d.used_llm ? "yes" : "no"],
      ["Source", d.email ? 'email "' + d.email.subject + '" from ' + d.email.sender : d.source],
      ["Updated", d.updated_at],
    ]),
    d.error ? el("p", { class: "error" }, d.error) : null,
    reviewBlock(d, resolved),
  ];
  if (d.issues.length) {
    parts.push(el("h3", {}, "Issues"), el("ul", { class: "issues" }, d.issues.map((i) =>
      el("li", { class: i.severity === "error" ? "sev-error" : "sev-warning" },
        el("strong", {}, i.severity.toUpperCase() + " " + i.code), ": " + i.message))));
  }
  if (d.validation) {
    const skipped = Object.entries(d.validation.skipped);
    parts.push(el("h3", {}, "Checks"),
      el("div", { class: "chips" },
        d.validation.passed.map((c) => el("span", { class: "chip ok" }, c + " ✓")),
        skipped.map(([c]) => el("span", { class: "chip skip" }, c + " (skipped)"))),
      skipped.length ? el("ul", { class: "sub" }, skipped.map(([c, why]) => el("li", {}, c + ": " + why))) : null);
  }
  if (st) {
    const summary = Object.entries(st.summary).filter(([, v]) => v !== null);
    parts.push(el("h3", {}, "Summary"), kv(summary.map(([k, v]) => [k.replaceAll("_", " "), fmt(v)])));
    if (st.holdings.length) {
      parts.push(el("h3", {}, "Holdings (" + st.holdings.length + ")"),
        table(["Security", "ISIN", "Quantity", "Cost", "Price", "Value", "Weight %"],
          st.holdings.map((h) => ({ cells: [h.security_name, txt(h.isin), fmt(h.quantity, 0),
            fmt(h.cost), fmt(h.market_price), fmt(h.market_value), fmt(h.weight_pct)] })),
          [2, 3, 4, 5, 6]));
    }
    if (st.transactions.length) {
      parts.push(el("h3", {}, "Transactions (" + st.transactions.length + ")"),
        table(["Date", "Type", "Security", "Quantity", "Amount"],
          st.transactions.map((t) => ({ cells: [t.date, t.type, txt(t.security_name),
            fmt(t.quantity, 0), fmt(t.amount)] })),
          [3, 4]));
    }
  }
  return el("div", { class: "panel" }, parts);
}

// ------------------------------------------------------------------ tabs
async function renderDocuments(p) {
  const params = new URLSearchParams({ limit: PAGE, offset: state.page * PAGE });
  if (state.filter === "attention") params.set("attention", "true");
  if (state.filter === "ok") params.set("status", "parsed_ok");
  if (state.q) params.set("q", state.q);
  const data = await api("/api/documents?" + params);
  if (p.stale()) return;
  if (!data.items.length && data.total > 0 && state.page > 0) {   // the last page vanished
    state.page = Math.ceil(data.total / PAGE) - 1;
    return renderDocuments(p);
  }
  p.filters.replaceChildren(filterBar([["attention", "Needs attention"], ["all", "All"], ["ok", "Parsed OK"]]));
  p.list.replaceChildren(data.items.length
    ? table(["Status", "File", "Provider", "Client", "Period", "Closing value", "Format", "Notes"],
      data.items.map((d) => ({
        selected: d.id === state.selected,
        onclick: () => { state.selected = d.id; render(); },
        cells: [
          el("div", {}, badge(d.status), el("div", { class: "row" }, reviewPill(d.review))),
          d.filename,
          el("div", {}, txt(d.provider), el("div", { class: "sub" }, txt(d.strategy_name))),
          txt(d.client_id),
          d.period_start ? d.period_start + " → " + d.period_end : "–",
          d.closing_value == null ? "–" : (d.currency || "") + " " + fmt(d.closing_value),
          el("div", {}, txt(d.format_name),
            el("div", { class: "sub" }, (d.match_kind || "–") + " · LLM " + (d.used_llm ? "yes" : "no"))),
          el("div", {}, d.headline ? trunc(d.headline, 90) : null,
            el("div", { class: "sub" }, d.errors + " error(s), " + d.warnings + " warning(s)")),
        ],
      })), [5])
    : el("div", { class: "empty" }, state.q ? "No documents match your search."
      : state.filter === "attention" ? "Nothing needs attention." : "No documents yet."));
  renderPager(p.pager, data);
  if (!p.poll) {          // auto-refresh must not wipe a note someone is typing
    p.detail.replaceChildren(state.selected ? await docDetail(state.selected) : "");
  }
}

async function renderEmails(p) {
  const emails = await api("/api/emails?include_no_pdf=" + state.showNoPdf);
  if (p.stale()) return;
  const box = el("input", { type: "checkbox",
    onchange: (e) => { state.showNoPdf = e.target.checked; render(); } });
  box.checked = state.showNoPdf;
  p.filters.replaceChildren(el("label", { class: "check" }, box, " Show emails without PDFs"));
  p.pager.replaceChildren();
  if (!p.poll) p.detail.replaceChildren();
  p.list.replaceChildren(emails.length
    ? table(["Received", "From", "Subject", "Decision", "Attachments"], emails.map((m) => ({
      cells: [
        txt(m.received && m.received.slice(0, 16).replace("T", " ")), txt(m.sender), txt(m.subject), m.decision,
        el("div", {}, m.attachments.map((a) => el("div", { title: a.reasons.join("; ") },
          a.filename + " · score " + a.score + " · " +
          (a.status ? a.status + (a.used_llm ? " (LLM)" : "") : (a.accepted ? "accepted" : "skipped"))))),
      ],
    })))
    : el("div", { class: "empty" }, "No statement emails recorded yet."));
}

async function renderFormats(p) {
  const formats = await api("/api/formats");
  if (p.stale()) return;
  p.filters.replaceChildren(el("span", { class: "hint" },
    "Layouts the system has learned. Each is a stored config; click one to see it."));
  p.pager.replaceChildren();
  p.list.replaceChildren(formats.length
    ? table(["Name", "Version", "Learned at", "Fingerprint labels"], formats.map((f) => ({
      selected: f.id === state.selected,
      onclick: () => { state.selected = f.id; render(); },
      cells: [f.name, f.version, f.created_at, f.label_count],
    })), [1, 3])
    : el("div", { class: "empty" }, "No formats learned yet."));
  if (p.poll) return;
  if (!state.selected) { p.detail.replaceChildren(); return; }
  const f = await api("/api/formats/" + state.selected);
  p.detail.replaceChildren(el("div", { class: "panel" },
    el("div", { class: "drawer-head" }, el("h2", {}, f.name + " v" + f.version),
      el("button", { class: "ghost", onclick: () => { state.selected = null; render(); } }, "Close")),
    el("h3", {}, "Stored config (written by the LLM, accepted only after four checks)"),
    el("pre", {}, JSON.stringify(f.config, null, 2)),
    el("h3", {}, "Fingerprint labels"),
    el("pre", {}, f.labels.join("\n"))));
}

async function render(opts) {
  const seq = ++renderSeq;
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === state.tab));
  $("search").hidden = state.tab !== "documents";
  const p = {
    filters: $("filters"), list: $("list"), pager: $("pager"), detail: $("detail"),
    poll: !!(opts && opts.poll), stale: () => seq !== renderSeq,
  };
  try {
    await renderCards();
    if (state.tab === "documents") await renderDocuments(p);
    else if (state.tab === "emails") await renderEmails(p);
    else await renderFormats(p);
    $("layout").classList.toggle("has-detail", p.detail.childElementCount > 0);
    $("updated").textContent = "Updated " + new Date().toLocaleTimeString();
  } catch (e) {
    if (e instanceof AuthError) return;
    p.list.replaceChildren(el("p", { class: "error" }, "Could not load data: " + e.message));
  }
}

// ------------------------------------------------------------------ wiring
document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => go(b.dataset.tab)));
$("refresh").addEventListener("click", () => render());
$("signout").addEventListener("click", () => {
  sessionStorage.removeItem("review_token");
  token = "";
  showLogin("");
});
$("login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  token = $("token").value.trim();
  sessionStorage.setItem("review_token", token);
  $("login").hidden = true;
  render();
});
let searchTimer = null;
$("search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = e.target.value.trim();
    state.page = 0;
    state.selected = null;
    render();
  }, 300);
});
setInterval(() => {
  if (token && !document.hidden && $("login").hidden) render({ poll: true });
}, POLL_MS);

if (token) render(); else showLogin("");
