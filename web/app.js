// Trading Bot PWA. Talks to /api/* on the same origin.

const KEY_API = "tb_api_key";
const KEY_TAB = "tb_tab";
const KEY_EQUITY = "tb_equity";

const state = {
  tab: localStorage.getItem(KEY_TAB) || "dashboard",
  apiKey: localStorage.getItem(KEY_API) || "",
  equity: parseFloat(localStorage.getItem(KEY_EQUITY) || "0") || 0,
  status: null,
  ws: null,
  wsBackoff: 1000,
};

function $(sel, root = document) { return root.querySelector(sel); }
function el(tag, attrs = {}, kids = []) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const kid of [].concat(kids)) {
    if (kid == null) continue;
    n.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
  }
  return n;
}
function fmtUsd(x) {
  if (x == null || isNaN(x)) return "-";
  const v = Number(x);
  if (Math.abs(v) >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
  if (Math.abs(v) >= 1e3) return "$" + (v / 1e3).toFixed(1) + "k";
  return "$" + v.toFixed(2);
}
function fmtPct(x, d = 1) {
  if (x == null || isNaN(x)) return "-";
  return (x * 100).toFixed(d) + "%";
}
function fmtBps(x, d = 1) {
  if (x == null || isNaN(x)) return "-";
  return x.toFixed(d) + "b";
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (state.apiKey) headers["X-Api-Key"] = state.apiKey;
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (_) {}
    throw new Error(resp.status + ": " + detail);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

function setStatus(s) {
  state.status = s;
  const dot = $("#connDot");
  const pill = $("#statusPill");
  if (!s) {
    dot.className = "dot"; pill.className = "status-pill"; pill.textContent = "offline";
    return;
  }
  if (s.running) {
    dot.className = "dot ok";
    pill.className = "status-pill on";
    pill.textContent = (s.dry_run ? "paper" : "live") + " · running";
  } else {
    dot.className = "dot warn";
    pill.className = "status-pill";
    pill.textContent = "stopped";
  }
}

function connectWs() {
  if (state.ws) try { state.ws.close(); } catch (_) {}
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = state.apiKey ? "?api_key=" + encodeURIComponent(state.apiKey) : "";
  const ws = new WebSocket(proto + "://" + location.host + "/ws" + q);
  state.ws = ws;
  ws.onmessage = (msg) => {
    try {
      const obj = JSON.parse(msg.data);
      if (obj.type === "status") {
        setStatus(obj.data);
        if (state.tab === "dashboard") renderDashboard();
      }
    } catch (_) {}
  };
  ws.onclose = () => {
    state.ws = null;
    setTimeout(connectWs, state.wsBackoff);
    state.wsBackoff = Math.min(state.wsBackoff * 2, 30000);
  };
  ws.onopen = () => { state.wsBackoff = 1000; };
}

// ---------- views ----------

function renderDashboard() {
  const root = $("#view");
  root.replaceChildren();
  const s = state.status;

  const eq = s ? s.equity_usd : 0;
  const start = s ? s.day_start_equity : 0;
  const dayPnl = start > 0 ? (eq - start) / start : 0;

  root.appendChild(el("section", { class: "card" }, [
    el("h2", {}, "Account"),
    metric("Equity", fmtUsd(eq)),
    metric("Free", fmtUsd(s ? s.free_usd : 0)),
    metric("Day PnL", fmtPct(dayPnl, 2), dayPnl >= 0 ? "up" : "down"),
    metric("Mode", s ? (s.dry_run ? "paper (dry-run)" : "live") : "-"),
    metric("Exchange", s ? `${s.exchange} (${s.market_type})` : "-"),
  ]));

  const positions = s && s.positions ? s.positions : [];
  const card = el("section", { class: "card" }, [el("h2", {}, "Positions")]);
  if (!positions.length) card.appendChild(el("div", { class: "empty" }, "no open positions"));
  else positions.forEach(p => {
    card.appendChild(el("div", { class: "metric" }, [
      el("span", { class: "label" }, `${p.symbol} ${p.side}`),
      el("span", { class: "value " + (p.pnl_bps >= 0 ? "up" : "down") },
        `${fmtUsd(p.amount * (p.mark || p.entry_price))} · ${fmtBps(p.pnl_bps)}`),
    ]));
  });
  root.appendChild(card);

  const ctrl = el("section", { class: "card" }, [
    el("h2", {}, "Controls"),
    el("div", { class: "row-actions" }, [
      el("button", {
        class: "primary",
        disabled: s && s.running ? "" : null,
        onclick: async () => { try { setStatus(await api("/api/start", { method: "POST" })); } catch (e) { alert(e.message); } },
      }, "Start"),
      el("button", {
        class: "danger",
        disabled: !s || !s.running ? "" : null,
        onclick: async () => { try { setStatus(await api("/api/stop", { method: "POST" })); } catch (e) { alert(e.message); } },
      }, "Stop"),
    ]),
    el("p", { class: "refresh" },
      "Always paper-trade for days first. The bot will not emit signals until profiles meet the configured sample size."),
  ]);
  root.appendChild(ctrl);
}

function metric(label, value, valClass) {
  return el("div", { class: "metric" }, [
    el("span", { class: "label" }, label),
    el("span", { class: "value" + (valClass ? " " + valClass : "") }, value),
  ]);
}

async function renderTraders() {
  const root = $("#view");
  root.replaceChildren(el("div", { class: "card" }, [
    el("h2", {}, "Trader profiles"),
    el("div", { class: "empty" }, "loading…"),
  ]));
  let rows = [];
  try {
    const url = "/api/traders?limit=20" + (state.equity > 0 ? "&equity=" + state.equity : "");
    rows = await api(url);
  } catch (e) {
    root.replaceChildren(el("div", { class: "card" }, [
      el("h2", {}, "Trader profiles"),
      el("div", { class: "error" }, e.message),
    ]));
    return;
  }
  root.replaceChildren();
  if (!rows.length) {
    root.appendChild(el("div", { class: "card" }, [
      el("h2", {}, "Trader profiles"),
      el("div", { class: "empty" },
        "No qualified profiles yet. Let the bot watch the tape — profiles need enough settled events first."),
    ]));
    return;
  }
  const card = el("section", { class: "card" }, [el("h2", {}, "Top profiles by edge × sample")]);
  rows.forEach(r => card.appendChild(profileRow(r)));
  root.appendChild(card);
}

function profileRow(r) {
  const pairs = (r.pairs || []).slice(0, 3)
    .map(p => `${p.symbol} (${p.events})`).join(" · ") || "-";
  const skewBuy = Math.round((r.buy_pct || 0) * 100);
  const skew = `B ${skewBuy} / S ${100 - skewBuy}`;
  const exp = r.expectancy_bps || 0;
  const rec = r.recommendation;

  const head = el("div", { class: "profile-head" }, [
    el("span", { class: "profile-id" }, r.profile_id),
    el("span", { class: "exp " + (exp >= 0 ? "pos" : "neg") },
      `${fmtPct(r.win_rate)} · ${fmtBps(exp)}`),
  ]);
  const kvs = el("div", { class: "kvs" }, [
    el("span", { class: "k" }, "events"), el("span", { class: "v" }, String(r.total)),
    el("span", { class: "k" }, "side skew"), el("span", { class: "v" }, skew),
    el("span", { class: "k" }, "lot $"),
    el("span", { class: "v" }, `${fmtUsd(r.lot_notional_min)}–${fmtUsd(r.lot_notional_max)}`),
    el("span", { class: "k" }, "trade $"),
    el("span", { class: "v" }, `${fmtUsd(r.trade_notional_min)}–${fmtUsd(r.trade_notional_max)}`),
    el("span", { class: "k" }, "you should size"),
    el("span", { class: "v" }, rec
      ? (rec.approved
          ? `${fmtPct(rec.equity_pct, 2)} · ${fmtUsd(rec.usd_notional)}`
          : `skip: ${rec.reason}`)
      : "no live mark"),
  ]);
  return el("div", { class: "profile-row" }, [
    head,
    el("div", { class: "pairs" }, "pairs: " + pairs),
    kvs,
  ]);
}

async function renderEvents() {
  const root = $("#view");
  root.replaceChildren(el("div", { class: "card" }, [
    el("h2", {}, "Recent events"),
    el("div", { class: "empty" }, "loading…"),
  ]));
  let rows = [];
  try { rows = await api("/api/events?limit=50"); }
  catch (e) {
    root.replaceChildren(el("div", { class: "card" }, [
      el("h2", {}, "Recent events"),
      el("div", { class: "error" }, e.message),
    ]));
    return;
  }
  let fills = [];
  try { fills = await api("/api/fills?limit=20"); } catch (_) {}

  root.replaceChildren();
  const evCard = el("section", { class: "card" }, [el("h2", {}, "Whale events")]);
  if (!rows.length) evCard.appendChild(el("div", { class: "empty" }, "no events yet"));
  rows.forEach(e => {
    const ts = new Date(e.end_ms).toLocaleTimeString();
    const out = e.outcome_bps == null ? "pending" : fmtBps(e.outcome_bps);
    const cls = e.outcome_bps == null ? "" : (e.outcome_bps >= 0 ? "up" : "down");
    evCard.appendChild(el("div", { class: "event-row" }, [
      el("div", { class: "profile-head" }, [
        el("span", { class: "profile-id" }, `${e.symbol} ${e.side.toUpperCase()}`),
        el("span", { class: "value " + cls }, out),
      ]),
      el("div", { class: "pairs" },
        `${ts} · ${fmtUsd(e.notional_usd)} · profile ${e.profile_id} · ${e.prints} prints`),
    ]));
  });
  root.appendChild(evCard);

  const fillCard = el("section", { class: "card" }, [el("h2", {}, "Recent fills")]);
  if (!fills.length) fillCard.appendChild(el("div", { class: "empty" }, "no fills yet"));
  fills.forEach(f => {
    const ts = new Date(f.ts_ms).toLocaleTimeString();
    fillCard.appendChild(el("div", { class: "event-row" }, [
      el("div", { class: "profile-head" }, [
        el("span", { class: "profile-id" }, `${f.symbol} ${f.side}`),
        el("span", { class: "value" }, fmtUsd(f.price * f.amount)),
      ]),
      el("div", { class: "pairs" },
        `${ts} · ${f.amount} @ ${f.price} · ${(f.meta && f.meta.reason) || ""}`),
    ]));
  });
  root.appendChild(fillCard);
}

function renderSettings() {
  const root = $("#view");
  root.replaceChildren();

  root.appendChild(el("section", { class: "card" }, [
    el("h2", {}, "API"),
    el("label", { class: "field" }, [
      el("span", { class: "lbl" }, "API key (must match server's API_KEY env)"),
      el("input", {
        class: "input", type: "password", value: state.apiKey,
        oninput: (e) => { state.apiKey = e.target.value; },
      }),
    ]),
    el("label", { class: "field" }, [
      el("span", { class: "lbl" }, "Equity for sizing preview ($)"),
      el("input", {
        class: "input", type: "number", inputmode: "decimal", min: "0",
        value: state.equity ? String(state.equity) : "",
        oninput: (e) => { state.equity = parseFloat(e.target.value) || 0; },
      }),
    ]),
    el("button", {
      class: "primary",
      onclick: () => {
        localStorage.setItem(KEY_API, state.apiKey);
        localStorage.setItem(KEY_EQUITY, String(state.equity));
        connectWs();
        setTab("dashboard");
      },
    }, "Save"),
  ]));

  root.appendChild(el("section", { class: "card" }, [
    el("h2", {}, "Install"),
    el("p", { class: "pairs" }, "iOS Safari: Share → Add to Home Screen."),
    el("p", { class: "pairs" }, "Android Chrome: ⋮ menu → Install app."),
  ]));

  const cfgCard = el("section", { class: "card" }, [
    el("h2", {}, "Server config"),
    el("div", { class: "empty" }, "loading…"),
  ]);
  root.appendChild(cfgCard);
  api("/api/config")
    .then(c => {
      cfgCard.replaceChildren(el("h2", {}, "Server config"));
      cfgCard.appendChild(metric("symbols", (c.symbols || []).join(", ") || "-"));
      cfgCard.appendChild(metric("min whale $", fmtUsd(c.whale_detector.min_notional_usd)));
      cfgCard.appendChild(metric("min events", String(c.whale_tracker.min_events_for_scoring)));
      cfgCard.appendChild(metric("min win rate", fmtPct(c.signals.min_win_rate, 0)));
      cfgCard.appendChild(metric("max position", fmtPct(c.risk.max_position_pct)));
      cfgCard.appendChild(metric("daily DD halt", fmtPct(c.risk.daily_drawdown_halt_pct)));
      cfgCard.appendChild(metric("dry run", c.execution.dry_run ? "yes" : "no"));
    })
    .catch(e => {
      cfgCard.replaceChildren(el("h2", {}, "Server config"),
        el("div", { class: "error" }, e.message));
    });
}

// ---------- routing ----------

function setTab(name) {
  state.tab = name;
  localStorage.setItem(KEY_TAB, name);
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.tab === name));
  if (name === "dashboard") renderDashboard();
  else if (name === "traders") renderTraders();
  else if (name === "events") renderEvents();
  else if (name === "settings") renderSettings();
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".tab").forEach(b =>
    b.addEventListener("click", () => setTab(b.dataset.tab)));
  setTab(state.tab);
  api("/api/status").then(setStatus).catch(() => setStatus(null));
  connectWs();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
});
