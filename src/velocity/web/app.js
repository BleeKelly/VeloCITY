"use strict";

/* ---------- tiny DOM helpers (data always goes in as text, never HTML) ---------- */

const SVGNS = "http://www.w3.org/2000/svg";

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid != null) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}

const $ = (sel) => document.querySelector(sel);
const fmt1 = (x) => (x == null ? "–" : x.toFixed(1));
const fmt0 = (x) => (x == null ? "–" : Math.round(x).toString());
const signed = (x, d = 1) => {
  if (x == null) return "–";
  const r = Math.abs(x).toFixed(d);
  return +r === 0 ? r : (x > 0 ? "+" : "−") + r;
};
const pct = (x) => (x == null ? "–" : Math.round(x * 100) + "%");
const ordinal = (n) => n + (["th", "st", "nd", "rd"][((n % 100) - 20) % 10] || ["th", "st", "nd", "rd"][n % 100] || "th");
const DOWN = ["", "1st", "2nd", "3rd", "4th"];
const cleanDesc = (d) => (d || "").replace(/^\(\s*:?\d*:?\d+\)\s*/, "");

/* ---------- data ---------- */

const store = { summary: null, variant: "all", scope: "history", timers: [] };
// Rating set key on the server: all/ng (garbage time) + "_season" for this-season-only ratings.
const setKey = () => store.variant + (store.scope === "season" ? "_season" : "");
const toggles = (rerender) => [
  seg([["history", "Full history"], ["season", "This season only"]], store.scope, (v) => { store.scope = v; rerender(); }),
  seg([["all", "All plays"], ["ng", "No garbage time"]], store.variant, (v) => { store.variant = v; rerender(); }),
];

async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await res.json().catch(() => ({}));
  if (res.status === 503) throw Object.assign(new Error("building"), { building: true, status: body.status });
  if (!res.ok) throw Object.assign(new Error(body.error || res.statusText), { code: res.status });
  return body;
}

function meta(abbr) {
  return (store.summary && store.summary.teams[abbr]) || { name: abbr, short: abbr, color: "#777777", alt: "#999999" };
}

function logo(abbr, size = "") {
  const m = meta(abbr);
  if (m.logo) {
    const img = h("img", { class: `logo ${size}`, src: m.logo, alt: "", loading: "lazy" });
    img.addEventListener("error", () => img.replaceWith(fallbackLogo(abbr, size)), { once: true });
    return img;
  }
  return fallbackLogo(abbr, size);
}

function fallbackLogo(abbr, size) {
  return h("span", { class: `logo logo-fallback ${size}`, style: { background: meta(abbr).color }, "aria-hidden": "true" }, abbr);
}

function teamLink(abbr, extra) {
  return h("a", { href: `/team/${abbr}`, "data-link": true, class: "team-cell" }, logo(abbr),
    h("span", {}, h("span", { class: "name" }, abbr), extra ? h("div", { class: "coach" }, extra) : null));
}

/* ---------- tooltip ---------- */

const tip = {
  show(evt, build) {
    const el = $("#tooltip");
    el.replaceChildren(...build());
    el.hidden = false;
    const pad = 14, r = el.getBoundingClientRect();
    let x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - pad;
    if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - pad;
    el.style.left = Math.max(8, x) + "px";
    el.style.top = Math.max(8, y) + "px";
  },
  hide() { $("#tooltip").hidden = true; },
};

function ttRow(color, value, label) {
  return h("div", { class: "tt-row" }, h("span", { class: "tt-key", style: { background: color } }), h("strong", {}, value), h("span", { class: "ink-2" }, label));
}

/* ---------- charts ---------- */

function niceTicks(lo, hi, count = 5) {
  const span = hi - lo || 1;
  const step0 = span / count, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((st) => span / st <= count) || 10 * mag;
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) ticks.push(+v.toFixed(6));
  return ticks;
}

/**
 * Line chart with a crosshair tooltip.
 * opts: points[], series[{key,label,color}], height, yFormat, zero, xMarks(points)->[{i,label,major}],
 *       title(point) -> string, extra(point) -> Node|null
 */
let chartSeq = 0;

function lineChart(opts) {
  const uid = ++chartSeq;
  const wrap = h("div", { class: "chart" });
  const legend = h("div", { class: "legend" }, opts.series.map((se) =>
    h("span", { class: "key" }, h("span", { class: "line-key", style: { background: se.color } }), se.label)));
  const holder = h("div", {});
  wrap.append(...(opts.legend === false ? [] : [legend]), holder);

  function render() {
    const W = Math.max(280, holder.clientWidth || 600), H = opts.height || 280;
    const m = opts.compact ? { l: 30, r: 6, t: 6, b: 6 } : { l: 44, r: opts.endLabels === false ? 12 : 46, t: 10, b: 26 };
    const pts = opts.points, n = pts.length;
    if (!n) { holder.replaceChildren(h("div", { class: "empty" }, "No games yet.")); return; }
    const vals = pts.flatMap((p) => opts.series.map((se) => p[se.key])).filter((v) => v != null);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (opts.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    if (opts.symmetric) { const a = Math.max(Math.abs(lo), Math.abs(hi)); lo = -a; hi = a; }
    if (opts.yDomain) [lo, hi] = opts.yDomain;
    const padY = (hi - lo || 10) * 0.08; lo -= padY; hi += padY;
    const ticks = niceTicks(lo, hi, opts.compact ? 3 : 5);
    lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
    const x = (i) => m.l + (n === 1 ? (W - m.l - m.r) / 2 : (i * (W - m.l - m.r)) / (n - 1));
    const y = (v) => m.t + ((hi - v) * (H - m.t - m.b)) / (hi - lo);

    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": opts.ariaLabel || "line chart", tabindex: 0 });
    for (const t of ticks) {
      svg.append(s("line", { class: t === 0 && opts.zero ? "baseline" : "gridline", x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }));
      svg.append(s("text", { class: "axis-label", x: m.l - 8, y: y(t) + 4, "text-anchor": "end" }, (opts.yFormat || fmt0)(t, ticks[1] - ticks[0])));
    }
    let lastLabelX = -1e9;
    for (const mk of (opts.xMarks ? opts.xMarks(pts) : [])) {
      const xx = x(mk.i);
      if (mk.major) svg.append(s("line", { class: "season-line", x1: xx, x2: xx, y1: m.t, y2: H - m.b }));
      if (xx - lastLabelX >= (mk.minGap || 34)) {
        svg.append(s("text", { class: "axis-label", x: xx + (mk.major ? 3 : 0), y: H - 8, "text-anchor": mk.major ? "start" : "middle" }, mk.label));
        lastLabelX = xx;
      }
    }
    if (opts.fillZero) {
      // One zero-sum series: tint the area toward whichever side is ahead.
      const se = opts.series[0], z = y(0), f = opts.fillZero;
      const pts2 = pts.map((p, i) => [x(i), p[se.key]]).filter(([, v]) => v != null);
      const area = `M${pts2[0][0]},${z}` + pts2.map(([xx, v]) => `L${xx.toFixed(1)},${y(v).toFixed(1)}`).join("") + `L${pts2[pts2.length - 1][0]},${z}Z`;
      const defs = s("defs", {},
        s("clipPath", { id: `up${uid}` }, s("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: Math.max(0, z - m.t) })),
        s("clipPath", { id: `dn${uid}` }, s("rect", { x: m.l, y: z, width: W - m.l - m.r, height: Math.max(0, H - m.b - z) })));
      svg.append(defs,
        s("path", { d: area, fill: f.above, "fill-opacity": 0.16, "clip-path": `url(#up${uid})` }),
        s("path", { d: area, fill: f.below, "fill-opacity": 0.16, "clip-path": `url(#dn${uid})` }));
      const tag = (yy, color, text) => [s("rect", { x: m.l + 8, y: yy - 9, width: 10, height: 10, rx: 2, fill: color }),
        s("text", { class: "end-label", x: m.l + 24, y: yy }, text)];
      svg.append(...tag(m.t + 14, f.above, `▲ ${f.upLabel}`), ...tag(H - m.b - 8, f.below, `▼ ${f.downLabel}`));
    }
    const ends = [];
    for (const se of opts.series) {
      let d = "", pen = false;
      pts.forEach((p, i) => {
        const v = p[se.key];
        if (v == null) { pen = false; return; }
        d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
        pen = true;
      });
      svg.append(s("path", { class: "series", d, stroke: se.color }));
      const last = [...pts].reverse().find((p) => p[se.key] != null);
      if (last) ends.push({ se, y: y(last[se.key]) });
    }
    // Direct end labels only when they don't collide; the legend always carries identity.
    const sorted = [...ends].sort((a, b) => a.y - b.y);
    if (opts.endLabels !== false && sorted.every((e, i) => i === 0 || e.y - sorted[i - 1].y >= 14)) {
      for (const e of ends) svg.append(s("text", { class: "end-label", x: W - m.r + 6, y: e.y + 4 }, e.se.label));
    }

    const cross = s("line", { class: "crosshair", y1: m.t, y2: H - m.b, visibility: "hidden" });
    const dots = opts.series.map((se) => s("circle", { class: "hover-dot", r: 4.5, fill: se.color, visibility: "hidden" }));
    const hit = s("rect", { x: m.l - 6, y: 0, width: W - m.l - m.r + 12, height: H, fill: "transparent" });
    svg.append(cross, ...dots, hit);

    let cur = -1;
    function focus(i, evt) {
      cur = Math.max(0, Math.min(n - 1, i));
      const p = pts[cur], xx = x(cur);
      cross.setAttribute("x1", xx); cross.setAttribute("x2", xx); cross.setAttribute("visibility", "visible");
      opts.series.forEach((se, k) => {
        const v = p[se.key];
        if (v == null) { dots[k].setAttribute("visibility", "hidden"); return; }
        dots[k].setAttribute("cx", xx); dots[k].setAttribute("cy", y(v)); dots[k].setAttribute("visibility", "visible");
      });
      const r = svg.getBoundingClientRect();
      const anchor = evt && evt.clientX != null ? evt : { clientX: r.left + (xx / W) * r.width, clientY: r.top + 20 };
      tip.show(anchor, () => [
        h("div", { class: "tt-title" }, opts.title ? opts.title(p) : ""),
        ...(opts.tipRows ? opts.tipRows(p).map((r) => ttRow(...r))
          : opts.series.filter((se) => p[se.key] != null).map((se) => ttRow(se.color, (opts.tipFormat || opts.yFormat || fmt1)(p[se.key]), se.label))),
        opts.extra ? opts.extra(p) : null,
      ].filter(Boolean));
    }
    function clear() {
      cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); tip.hide();
    }
    hit.addEventListener("pointermove", (evt) => {
      const r = svg.getBoundingClientRect();
      const px = ((evt.clientX - r.left) / r.width) * W;
      focus(n === 1 ? 0 : Math.round(((px - m.l) / (W - m.l - m.r)) * (n - 1)), evt);
    });
    hit.addEventListener("pointerleave", clear);
    hit.addEventListener("click", () => cur >= 0 && opts.onClick && opts.onClick(pts[cur]));
    svg.addEventListener("keydown", (evt) => {
      if (evt.key === "ArrowRight" || evt.key === "ArrowLeft") {
        evt.preventDefault(); focus((cur < 0 ? n - 1 : cur) + (evt.key === "ArrowRight" ? 1 : -1));
      } else if (evt.key === "Enter" && cur >= 0 && opts.onClick) opts.onClick(pts[cur]);
    });
    svg.addEventListener("blur", clear);
    holder.replaceChildren(svg);
  }

  const ro = new ResizeObserver(() => render());
  requestAnimationFrame(() => { render(); ro.observe(holder); });
  return wrap;
}

function sparkline(values) {
  const W = 96, H = 26;
  if (!values || values.length < 2) return h("span", { class: "muted" }, "–");
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1;
  const x = (i) => 2 + (i * (W - 6)) / (values.length - 1);
  const y = (v) => 3 + ((hi - v) * (H - 6)) / span;
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const last = values.length - 1;
  return s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" },
    s("path", { d, fill: "none", stroke: "var(--spark)", "stroke-width": 1.5, "stroke-linejoin": "round" }),
    s("circle", { cx: x(last), cy: y(values[last]), r: 3, fill: "var(--accent)", stroke: "var(--surface)", "stroke-width": 1.5 }));
}

function probBar(leftLabel, leftP, rightLabel, leftColor = "var(--series-2)", rightColor = "var(--series-1)") {
  const lp = Math.max(0.02, Math.min(0.98, leftP));
  return h("div", { class: "prob" },
    h("div", { class: "prob-bar", role: "img", "aria-label": `${leftLabel} ${pct(leftP)}, ${rightLabel} ${pct(1 - leftP)}` },
      h("span", { style: { flex: lp, background: leftColor } }), h("span", { style: { flex: 1 - lp, background: rightColor } })),
    h("div", { class: "prob-labels" }, h("span", {}, `${leftLabel} ${pct(leftP)}`), h("span", {}, `${pct(1 - leftP)} ${rightLabel}`)));
}

function seg(options, value, onChange) {
  const group = h("div", { class: "seg", role: "group" });
  group.append(...options.map(([val, label]) => h("button", {
    type: "button", "aria-pressed": String(val === value),
    onclick: (e) => {
      group.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === e.currentTarget)));
      onChange(val);
    },
  }, label)));
  return group;
}

function tile(label, value, detail, cls = "") {
  return h("div", { class: `tile ${cls}` }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), detail ? h("div", { class: "detail" }, detail) : null);
}

/* ---------- chrome: status pill, slate, team picker ---------- */

function renderChrome() {
  const sm = store.summary;
  const pill = $("#status-pill");
  if (!sm) return;
  const liveN = sm.scoreboard.filter((g) => g.state === "in").length;
  pill.replaceChildren(
    liveN ? h("span", { class: "live-dot", "aria-hidden": "true" }) : null,
    liveN ? `${liveN} live · ` : "",
    h("span", { class: "long" }, `Through ${sm.status.through.season} week ${sm.status.through.week}`),
    h("span", { class: "short" }, `${sm.status.through.season} W${sm.status.through.week}`),
  );
  pill.title = `Rebuilt ${sm.status.built_at || "–"}${sm.status.live_at ? ` · live checked ${sm.status.live_at}` : ""}`;

  const sel = $("#team-select");
  if (sel.options.length <= 1) {
    for (const abbr of Object.keys(sm.teams).sort()) sel.append(h("option", { value: abbr }, `${abbr} · ${sm.teams[abbr].short}`));
  }
  renderSlate();
}

function gameStatus(g) {
  if (g.state === "pre") {
    const d = new Date(g.kickoff);
    return d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
  }
  return g.status || (g.state === "post" ? "Final" : "");
}

function renderSlate() {
  const slate = $("#slate");
  const games = store.summary.scoreboard;
  slate.replaceChildren(...games.map((g) => {
    const fav = g.home_win_prob >= 0.5 ? [g.home_team, g.home_win_prob] : [g.away_team, 1 - g.home_win_prob];
    const done = g.state !== "pre";
    const row = (abbr, score, win) => h("div", { class: `slate-row ${win ? "winner" : ""}` }, logo(abbr), h("span", { class: "abbr" }, abbr), done ? h("span", { class: "score" }, fmt0(score)) : null);
    return h("a", { class: `slate-card ${g.state === "in" ? "is-live" : ""}`, href: `/game/${g.game_id}`, "data-link": true },
      row(g.away_team, g.away_score, done && g.away_score > g.home_score),
      row(g.home_team, g.home_score, done && g.home_score > g.away_score),
      h("div", { class: "slate-meta" },
        h("span", {}, g.state === "in" ? h("span", { class: "badge live" }, "Live") : null, " ", gameStatus(g)),
        g.home_win_prob != null ? h("span", {}, `${fav[0]} ${pct(fav[1])}`) : null));
  }));
}

/* ---------- views ---------- */

function setNav(name) {
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

const COLS = [
  { key: "net", label: "Net" }, { key: "spread", label: "Spread" }, { key: "off", label: "Offense" },
  { key: "def", label: "Defense" }, { key: "off_win_pct", label: "Off win %" }, { key: "def_win_pct", label: "Def win %" },
  { key: "coach", label: "Coach ⚠" },
];

function viewRatings(app, params) {
  setNav("ratings");
  const sm = store.summary;
  const ng = store.variant === "ng";
  const k = (key) => (ng && ["net", "off", "def", "spread", "net_rank", "off_rank", "def_rank"].includes(key) ? `${key}_ng` : key);
  const source = store.scope === "season" ? sm.ratings_season : sm.ratings;
  const anyLive = source.some((r) => Math.abs(r.live_change || 0) >= 0.05);
  let sortKey = params.get("sort") || "net", sortDir = -1;  // -1 = high to low
  const rows = [...source];
  const sortBy = (key) => {
    if (key === sortKey) sortDir = -sortDir;
    else { sortKey = key; sortDir = key === "team" ? 1 : -1; }
    draw();
  };
  const arrow = (key) => (key === sortKey ? (sortDir < 0 ? " ▼" : " ▲") : "");
  const ariaSort = (key) => (key === sortKey ? (sortDir < 0 ? "descending" : "ascending") : null);

  const top = [...rows].sort((a, b) => b[k("net")] - a[k("net")])[0];
  const bestO = [...rows].sort((a, b) => b[k("off")] - a[k("off")])[0];
  const bestD = [...rows].sort((a, b) => b[k("def")] - a[k("def")])[0];
  const met = sm.metrics[setKey()];

  const maxAbs = Math.max(...rows.map((r) => Math.abs(r[k("net")])));
  const table = h("table", {});
  const thead = h("thead", {});
  const tbody = h("tbody", {});
  table.append(thead, tbody);

  function draw() {
    rows.sort((a, b) => sortKey === "team" ? sortDir * a.team.localeCompare(b.team)
      : sortDir * ((a[k(sortKey)] ?? -1e9) - (b[k(sortKey)] ?? -1e9)));
    thead.replaceChildren(h("tr", {},
      h("th", {}, "#"),
      h("th", { class: "left", "aria-sort": ariaSort("team") }, h("button", { type: "button", onclick: () => sortBy("team") }, "Team" + arrow("team"))),
      COLS.map((c) => h("th", { "aria-sort": ariaSort(c.key) },
        h("button", { type: "button", onclick: () => sortBy(c.key) }, c.label + arrow(c.key)))),
      h("th", {}, `${sm.status.through.season} net`),
      anyLive ? h("th", {}, "Live Δ") : null));
    tbody.replaceChildren(...rows.map((r, i) => {
      const net = r[k("net")];
      const w = (Math.abs(net) / (maxAbs || 1)) * 42;
      return h("tr", { class: "row-link", onclick: (e) => { if (!e.target.closest("a")) go(`/team/${r.team}`); } },
        h("td", { class: "rank" }, r[k("net_rank")]),
        h("td", { class: "left" }, teamLink(r.team, r.head_coach)),
        h("td", { class: "num" }, h("span", { class: "net-bar" },
          h("strong", {}, signed(net)),
          h("span", { class: "track", "aria-hidden": "true" }, h("span", { class: "fill", style: { width: w + "px", left: net >= 0 ? "42px" : 42 - w + "px", background: net >= 0 ? "var(--series-1)" : "var(--series-2)" } })))),
        h("td", { class: "num" }, signed(r[k("spread")])),
        h("td", { class: "num" }, fmt1(r[k("off")]), h("span", { class: "rk" }, ordinal(r[k("off_rank")]))),
        h("td", { class: "num" }, fmt1(r[k("def")]), h("span", { class: "rk" }, ordinal(r[k("def_rank")]))),
        h("td", { class: "num" }, fmt1(r.off_win_pct)),
        h("td", { class: "num" }, fmt1(r.def_win_pct)),
        h("td", { class: "num muted" }, fmt1(r.coach), h("span", { class: "rk" }, ordinal(r.coach_rank))),
        h("td", {}, sparkline(r.spark)),
        anyLive ? h("td", { class: `num ${r.live_change > 0 ? "delta-up" : r.live_change < 0 ? "delta-down" : "muted"}` },
          Math.abs(r.live_change || 0) >= 0.05 ? signed(r.live_change) : "") : null);
    }));
  }
  draw();

  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Power ratings"),
        h("div", { class: "sub" }, store.scope === "season"
          ? `This season only: every team started ${sm.status.through.season} at 1500. Through week ${sm.status.through.week}.`
          : `Offense and defense Elo from every run, pass and punt since ${sm.first_season}. Through ${sm.status.through.season} week ${sm.status.through.week}.`)),
      h("div", { class: "head-tools" }, toggles(() => render({ keepScroll: true })))),
    h("div", { class: "tiles" },
      tile("Top team", h("span", {}, logo(top.team, "md"), " ", top.team, " ", signed(top[k("net")])), `${signed(top[k("spread")])} pts vs an average team`, "hero"),
      tile("Best offense", h("span", {}, logo(bestO.team, "md"), " ", bestO.team), `${fmt1(bestO[k("off")])} Elo`),
      tile("Best defense", h("span", {}, logo(bestD.team, "md"), " ", bestD.team), `${fmt1(bestD[k("def")])} Elo`),
      tile("Picks the winner", `${met.game_pick_pct.toFixed(1)}%`, `${met.games.toLocaleString()} games since ${met.from_season} · home team wins ${met.home_win_pct.toFixed(1)}%`)),
    h("div", { class: "card" },
      h("div", { class: "table-wrap" }, table),
      h("p", { class: "card-sub", style: { margin: "12px 0 0" } },
        "Net = offense + defense Elo above average. Spread = points better than an average team on a neutral field. Off/Def win % = share of plays won this season (ties count half). Click a column to sort; click again to flip."),
      h("div", { class: "disclaimer" }, h("strong", {}, "⚠ Coach"), h("span", {}, sm.coach_disclaimer))),
  );
}

async function viewTeam(app, abbr, params) {
  setNav("");
  const data = await api(`/api/team/${abbr}?variant=${setKey()}`);
  const m = data.meta || meta(abbr);
  const cur = data.current;
  const hist = data.history.map((g) => ({ ...g, off: g.off_post - 1500, def: g.def_post - 1500, coach: g.coach_post - 1500 }));
  const seasons = [...new Set(hist.map((g) => g.season))];
  const lastSeason = seasons[seasons.length - 1];
  let range = params.get("season") ? "1" : params.get("range") || "5";
  let chartSeason = +(params.get("season") || lastSeason);
  let logSeason = chartSeason;

  const chartBox = h("div", {});
  const coachBox = h("div", {});
  const logBox = h("div", {});

  function rangePoints() {
    if (range === "1") return hist.filter((g) => g.season === chartSeason);
    if (range === "all") return hist;
    const keep = seasons.slice(-Number(range));
    return hist.filter((g) => keep.includes(g.season));
  }
  function drawCharts() {
    const pts = rangePoints();
    const oneSeason = new Set(pts.map((p) => p.season)).size === 1;
    const xMarks = (ps) => {
      if (oneSeason) return ps.map((p, i) => ({ i, label: p.week > 18 ? "P" : `W${p.week}`, minGap: 26 }));
      const marks = [];
      ps.forEach((p, i) => { if (i === 0 || p.season !== ps[i - 1].season) marks.push({ i, label: String(p.season), major: true, minGap: 40 }); });
      return marks;
    };
    const title = (p) => `${p.season} ${p.week > 18 ? "playoffs" : `week ${p.week}`} · ${p.home ? "vs" : "@"} ${p.opponent} · ${p.points_for > p.points_against ? "W" : p.points_for < p.points_against ? "L" : "T"} ${fmt0(p.points_for)}–${fmt0(p.points_against)}${p.provisional ? " · live" : ""}`;
    chartBox.replaceChildren(
      h("div", { class: "chart-head" },
        h("div", {}, h("h2", {}, "Ratings over time"), h("p", { class: "card-sub", style: { margin: 0 } }, "Elo above average after each game. Click a point to open the game.")),
        h("div", { class: "range", style: { display: "flex", gap: "8px", alignItems: "center" } },
          h("select", { class: "select", "aria-label": "Chart one season", onchange: (e) => pickSeason(+e.target.value) },
            [...seasons].reverse().map((y) => h("option", { value: y, selected: range === "1" && y === chartSeason ? true : null }, y))),
          seg([["1", "Season"], ["5", "5 yrs"], ["10", "10 yrs"], ["all", `All (${seasons[0]}–)`]], range, (v) => { range = v; drawCharts(); }))),
      lineChart({
        points: pts, zero: true, height: 300, ariaLabel: `${abbr} offense, defense and net ratings over time`,
        series: [{ key: "net", label: "Net", color: "var(--series-3)" }, { key: "off", label: "Offense", color: "var(--series-1)" }, { key: "def", label: "Defense", color: "var(--series-2)" }],
        yFormat: (v, step) => signed(v, step < 1 ? 1 : 0), tipFormat: (v) => signed(v), xMarks, title,
        onClick: (p) => go(`/game/${p.game_id}`),
      }));
    coachBox.replaceChildren(
      h("h2", {}, "Coaching staff ⚠"),
      h("p", { class: "card-sub" }, "Penalties, two-point tries and early timeouts."),
      lineChart({
        points: pts, zero: true, height: 160, legend: false, endLabels: false, ariaLabel: `${abbr} coaching rating over time`,
        series: [{ key: "coach", label: "Coach", color: "var(--series-1)" }],
        yFormat: (v, step) => signed(v, step < 1 ? 1 : 0), tipFormat: (v) => signed(v), xMarks: (ps) => xMarksCoarse(ps), title,
      }),
      h("div", { class: "disclaimer" }, h("span", {}, store.summary.coach_disclaimer)));
  }
  function pickSeason(y) {
    chartSeason = y; logSeason = y; range = "1";
    drawCharts(); drawLog();
  }
  function xMarksCoarse(ps) {
    const marks = [];
    ps.forEach((p, i) => { if (i === 0 || p.season !== ps[i - 1].season) marks.push({ i, label: String(p.season), major: true, minGap: 48 }); });
    return marks;
  }
  function drawLog() {
    const games = hist.filter((g) => g.season === logSeason);
    const sel = h("select", { class: "select", onchange: (e) => pickSeason(+e.target.value) },
      [...seasons].reverse().map((y) => h("option", { value: y, selected: y === logSeason ? true : null }, y)));
    logBox.replaceChildren(
      h("div", { class: "chart-head" }, h("h2", {}, "Game log"), h("label", { class: "range" }, h("span", { class: "sr-only" }, "Season"), sel)),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", { class: "left" }, "Week"), h("th", { class: "left" }, "Opponent"), h("th", {}, "Result"), h("th", {}, "Off"), h("th", {}, "Def"), h("th", {}, "Net"), h("th", {}, "Net Δ"))),
        h("tbody", {}, games.map((g) => {
          const d = g.net - g.net_pre;
          const res = g.points_for > g.points_against ? "W" : g.points_for < g.points_against ? "L" : "T";
          return h("tr", { class: "row-link", onclick: () => go(`/game/${g.game_id}`) },
            h("td", { class: "left" }, g.week > 18 ? `Playoffs (${g.week})` : `Week ${g.week}`, g.provisional ? h("span", { class: "badge prov", style: { marginLeft: "6px" } }, "live") : null),
            h("td", { class: "left" }, h("span", { class: "team-cell" }, h("span", { class: "muted" }, g.home ? "vs" : "@"), logo(g.opponent), g.opponent)),
            h("td", { class: "num" }, h("span", { class: `res-badge res-${res}` }, res), " ", `${fmt0(g.points_for)}–${fmt0(g.points_against)}`),
            h("td", { class: "num" }, signed(g.off)), h("td", { class: "num" }, signed(g.def)), h("td", { class: "num" }, h("strong", {}, signed(g.net))),
            h("td", { class: `num ${d > 0 ? "delta-up" : d < 0 ? "delta-down" : "muted"}` }, signed(d)));
        })))));
  }

  const seasonRows = [...data.seasons].reverse();
  drawCharts(); drawLog();

  // Off-season carryover: share of each rating's edge kept going into a season.
  const os = data.offseason || [];
  const latest = os[os.length - 1];
  const kept = (label, value, detail) => h("div", { class: "kept-row" },
    h("div", { class: "kept-head" }, h("span", {}, label), h("strong", { class: "num" }, pct(value))),
    h("div", { class: "meter", role: "img", "aria-label": `${label} kept ${pct(value)}` }, h("span", { style: { width: `${Math.round((value || 0) * 100)}%` } })),
    detail ? h("div", { class: "kept-detail" }, detail) : null);
  const coachNote = (r) => (r.coach_change === 1 ? "new head coach" : r.coach_change === 0 ? "same head coach" : "");
  const offseasonCard = latest ? h("div", { class: "card" },
    h("h2", {}, `${latest.season} off-season carryover`),
    h("p", { class: "card-sub" }, data.decay
      ? "Share of last season's edge each rating kept. Less turnover and an older lineup keep more; a new head coach resets the staff rating."
      : "Decay is off: every team keeps the same share."),
    kept("Offense", latest.off_kept, latest.off_cont != null ? `${pct(latest.off_cont)} of offensive snaps back · lineup age ${latest.off_age != null ? latest.off_age.toFixed(1) : "–"}` : null),
    kept("Defense", latest.def_kept, latest.def_cont != null ? `${pct(latest.def_cont)} of defensive snaps back · lineup age ${latest.def_age != null ? latest.def_age.toFixed(1) : "–"}` : null),
    kept("Coach ⚠", latest.coach_kept, coachNote(latest)),
    os.length > 1 ? h("div", { class: "table-wrap" }, h("table", { class: "ratings-move" },
      h("thead", {}, h("tr", {}, h("th", { class: "left" }, "Season"), h("th", {}, "Off kept"), h("th", {}, "Def kept"), h("th", {}, "Snaps back O/D"), h("th", {}, "Coach"))),
      h("tbody", {}, [...os].reverse().slice(1).map((r) => h("tr", {},
        h("td", { class: "left" }, r.season), h("td", { class: "num" }, pct(r.off_kept)), h("td", { class: "num" }, pct(r.def_kept)),
        h("td", { class: "num" }, r.off_cont != null ? `${pct(r.off_cont)} / ${pct(r.def_cont)}` : "–"),
        h("td", { class: "num muted" }, r.coach_change === 1 ? "new" : "")))))) : null) : null;
  app.replaceChildren(
    h("section", { class: "team-hero", style: { "--team": m.color, "--team-alt": m.alt } },
      h("div", { class: "title" }, logo(abbr, "lg"),
        h("div", {}, h("h1", {}, m.name || abbr), h("div", { class: "sub" }, `${cur.head_coach || ""} · ${ordinal(cur.net_rank)} of 32 by net rating${store.scope === "season" ? " this season" : ""}`)),
        h("div", { class: "head-tools" }, toggles(() => render({ keepScroll: true })))),
      h("div", { class: "tiles" },
        tile("Net", signed(cur.net), `${ordinal(cur.net_rank)} · ${signed(cur.spread)} pts vs average`, "hero"),
        tile("Offense", fmt1(cur.off), `${ordinal(cur.off_rank)} · wins ${fmt1(cur.off_win_pct)}% of plays`),
        tile("Defense", fmt1(cur.def), `${ordinal(cur.def_rank)} · wins ${fmt1(cur.def_win_pct)}% of plays`),
        tile("Coach ⚠", fmt1(cur.coach), `${ordinal(cur.coach_rank)} · it will suck`))),
    h("div", { class: "card" }, chartBox),
    h("div", { class: "grid-2" },
      h("div", { class: "card" }, logBox),
      h("div", {},
        offseasonCard,
        h("div", { class: "card" }, coachBox),
        h("div", { class: "card" }, h("h2", {}, "Seasons"),
          h("div", { class: "table-wrap", style: { maxHeight: "420px", overflowY: "auto" } }, h("table", {},
            h("thead", {}, h("tr", {}, h("th", { class: "left" }, "Season"), h("th", {}, "Record"), h("th", {}, "Net"), h("th", {}, "Off"), h("th", {}, "Def"))),
            h("tbody", {}, seasonRows.map((r) => h("tr", { class: "row-link", title: "Chart this season", onclick: () => { pickSeason(r.season); chartBox.scrollIntoView({ behavior: "smooth", block: "start" }); } },
              h("td", { class: "left" }, r.season),
              h("td", { class: "num" }, `${r.w}-${r.l}${r.t ? `-${r.t}` : ""}`),
              h("td", { class: "num" }, h("strong", {}, signed(r.net)), h("span", { class: "rk" }, ordinal(r.net_rank))),
              h("td", { class: "num" }, signed(r.off_post - 1500), h("span", { class: "rk" }, ordinal(r.off_post_rank))),
              h("td", { class: "num" }, signed(r.def_post - 1500), h("span", { class: "rk" }, ordinal(r.def_post_rank))))))))))),
  );
  document.title = `${abbr} · VeloCITY`;
}

async function viewGames(app, params) {
  setNav("games");
  const sm = store.summary;
  const season = +(params.get("season") || sm.status.through.season);
  const data = await api(`/api/games?season=${season}${params.get("week") ? `&week=${params.get("week")}` : ""}`);
  const seasonSel = h("select", { class: "select", onchange: (e) => go(`/games?season=${e.target.value}`) });
  for (let y = sm.status.through.season; y >= sm.first_season; y--) seasonSel.append(h("option", { value: y, selected: y === season ? true : null }, y));
  const weekSel = h("select", { class: "select", onchange: (e) => go(`/games?season=${season}&week=${e.target.value}`) },
    data.weeks.map((w) => h("option", { value: w, selected: w === data.week ? true : null }, w > 18 ? `Playoffs · ${w}` : `Week ${w}`)));

  const cards = data.games.map((g) => {
    const final = g.home_score != null;
    const homeWon = g.home_score > g.away_score, awayWon = g.away_score > g.home_score;
    const favHome = g.home_win_prob >= 0.5;
    const upset = final && ((favHome && awayWon) || (!favHome && homeWon));
    const side = (abbr, score, won, lost) => h("div", { class: `side ${lost ? "loser" : ""}` }, logo(abbr, "md"), h("span", { class: "tname" }, meta(abbr).short || abbr), h("span", { class: "score" }, final ? fmt0(score) : ""));
    return h("a", { class: "game-card", href: `/game/${g.game_id}`, "data-link": true },
      h("div", { class: "slate-meta" }, h("span", {}, g.location === "Neutral" ? "Neutral site" : `at ${g.home_team}`),
        h("span", {}, g.provisional ? h("span", { class: "badge prov" }, "Live data") : null, upset ? h("span", { class: "badge upset" }, "Upset") : null)),
      h("div", { class: "teams" }, side(g.away_team, g.away_score, awayWon, final && homeWon), side(g.home_team, g.home_score, homeWon, final && awayWon)),
      probBar(g.away_team, 1 - g.home_win_prob, g.home_team),
      h("div", { class: "prob-labels" }, h("span", {}, "Pregame chance"), h("span", {}, `Spread ${favHome ? g.home_team : g.away_team} ${signed(-Math.abs(g.home_spread))}`)));
  });

  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, `${season} ${data.week > 18 ? "playoffs" : `week ${data.week}`}`), h("div", { class: "sub" }, "Pregame chances come from the ratings going into each game.")),
      h("div", { class: "head-tools" }, seasonSel, weekSel)),
    cards.length ? h("div", { class: "games-grid" }, cards) : h("div", { class: "empty" }, "No games."));
  document.title = `${season} week ${data.week} · VeloCITY`;
}

async function viewGame(app, gameId) {
  setNav("games");
  let data;
  try {
    data = await api(`/api/game/${gameId}`);
  } catch (e) {
    const slate = store.summary.scoreboard.find((g) => g.game_id === gameId);
    if (e.code === 404 && slate) return viewPreview(app, slate);
    throw e;
  }
  const g = data.game, events = data.events;
  const home = g.home_team, away = g.away_team;
  const color = (abbr) => (abbr === home ? "var(--series-1)" : "var(--series-2)");
  const slate = store.summary.scoreboard.find((x) => x.game_id === gameId);
  const isLive = slate && slate.state === "in";
  const plays = events.filter((e) => e.kind === 0);
  let showCoach = false;

  // Zero-sum: every play moves one team's net rating up by exactly what the other loses, so the
  // swing is a single number. Positive = the home team gaining.
  let homeSwing = 0;
  const swing = plays.map((e, i) => {
    homeSwing += e.att_team === home ? e.delta : -e.delta;
    return { i, swing: homeSwing, e };
  });
  swing.unshift({ i: -1, swing: 0, e: null });
  const staffSwing = events.filter((e) => e.kind === 1).reduce((a, e) => a + (e.att_team === home ? e.delta : -e.delta), 0);
  const leader = (v) => (v >= 0 ? home : away);

  const finalHome = events.length ? events[events.length - 1].total_home_score : g.home_score;
  const finalAway = events.length ? events[events.length - 1].total_away_score : g.away_score;
  const hs = g.home_score ?? finalHome, as = g.away_score ?? finalAway;

  const moveRow = (side, abbr) => h("tr", {},
    h("td", { class: "left" }, teamLink(abbr)),
    ["off", "def", "coach"].map((u) => {
      const pre = g[`${side}_${u}_pre`], post = g[`${side}_${u}_post`], d = post - pre;
      return h("td", { class: "num" }, fmt1(post), " ", h("span", { class: d > 0 ? "delta-up" : d < 0 ? "delta-down" : "muted" }, `(${signed(d)})`));
    }));

  const tbody = h("tbody", {});
  function drawPlays() {
    const rows = [];
    let q = null;
    for (const e of events) {
      if (e.kind === 1 && !showCoach) continue;
      if (e.qtr !== q) { q = e.qtr; rows.push(h("tr", { class: "qtr-row" }, h("td", { colspan: 7 }, q >= 5 ? "Overtime" : `${ordinal(q)} quarter`))); }
      const isCoach = e.kind === 1;
      const res = e.y === 1 ? "W" : e.y === 0.5 ? "T" : "L";
      const dd = isCoach ? (e.event === "two_point" ? "2-pt try" : e.event === "timeout" ? "Timeout" : "Penalty")
        : e.play_type === "punt" ? `${DOWN[e.down] || ""} · punt` : `${DOWN[e.down] || ""} & ${fmt0(e.ydstogo)}`;
      rows.push(h("tr", { class: isCoach ? "coach-row" : "" },
        h("td", { class: "clock" }, e.time || ""),
        h("td", { class: "dd" }, dd),
        h("td", { class: "off" }, h("span", { class: "team-cell", title: isCoach ? `${e.att_team} staff vs ${e.def_team} staff` : `${e.att_team} offense vs ${e.def_team} defense` }, logo(e.att_team))),
        h("td", { class: "desc" }, isCoach ? `Coaching · ${cleanDesc(e.desc)}` : cleanDesc(e.desc)),
        h("td", { class: "chance" }, h("div", { class: "split" },
          h("div", { class: "split-bar", role: "img", "aria-label": `${e.att_team} ${pct(e.p)} to win the ${isCoach ? "event" : "play"}` },
            h("span", { style: { flex: Math.max(0.03, e.p), background: color(e.att_team) } }), h("span", { style: { flex: Math.max(0.03, 1 - e.p), background: color(e.def_team) } })),
          h("div", { class: "split-labels" }, h("span", {}, `${e.att_team} ${pct(e.p)}`), h("span", {}, `${pct(1 - e.p)} ${e.def_team}`)))),
        h("td", { class: "res" }, h("span", { class: `res-badge res-${res}`, title: `${e.att_team} ${res === "W" ? "won" : res === "T" ? "tied" : "lost"}` }, res)),
        h("td", { class: `elo ${e.delta > 0 ? "delta-up" : e.delta < 0 ? "delta-down" : "muted"}` }, signed(e.delta, 2))));
    }
    tbody.replaceChildren(...rows);
  }
  drawPlays();

  const offWins = (abbr) => {
    const mine = plays.filter((e) => e.att_team === abbr);
    return mine.length ? mine.reduce((a, e) => a + e.y, 0) / mine.length : null;
  };
  const expected = (abbr) => {
    const mine = plays.filter((e) => e.att_team === abbr);
    return mine.length ? mine.reduce((a, e) => a + e.p, 0) / mine.length : null;
  };

  app.replaceChildren(
    h("div", { class: "card", style: { marginTop: "18px" } },
      h("div", { class: "slate-meta", style: { marginBottom: "10px" } },
        h("span", {}, `${g.season} ${g.week > 18 ? "playoffs" : `week ${g.week}`} · ${g.game_date}${g.location === "Neutral" ? " · neutral site" : ""}`),
        h("span", {}, isLive ? h("span", { class: "badge live" }, "Live") : null, " ", data.provisional ? h("span", { class: "badge prov" }, "Provisional · ESPN feed") : null)),
      h("div", { class: "scoreboard" },
        h("a", { class: "side", href: `/team/${away}`, "data-link": true }, logo(away, "lg"), h("div", {}, h("div", { class: "tname" }, meta(away).short || away), h("div", { class: "muted" }, "Away")), h("span", { class: "big" }, fmt0(as))),
        h("div", { class: "mid" }, slate && slate.state !== "post" ? gameStatus(slate) : "Final"),
        h("a", { class: "side home", href: `/team/${home}`, "data-link": true }, h("span", { class: "big" }, fmt0(hs)), h("div", {}, h("div", { class: "tname" }, meta(home).short || home), h("div", { class: "muted" }, "Home")), logo(home, "lg"))),
      h("div", { style: { marginTop: "14px" } }, h("div", { class: "card-sub", style: { margin: "0 0 4px" } }, "Pregame chance to win"), probBar(away, 1 - g.home_win_prob, home))),
    h("div", { class: "tiles" },
      tile(`${away} offense`, `${pct(offWins(away))}`, `of plays won · expected ${pct(expected(away))}`),
      tile(`${home} offense`, `${pct(offWins(home))}`, `of plays won · expected ${pct(expected(home))}`),
      tile("Play swing", h("span", {}, logo(leader(homeSwing), "md"), ` ${leader(homeSwing)} ${signed(Math.abs(homeSwing))}`), "net Elo taken from the other team"),
      tile("Staff swing ⚠", h("span", {}, logo(leader(staffSwing), "md"), ` ${leader(staffSwing)} ${signed(Math.abs(staffSwing))}`), "coaching Elo from penalties, timeouts, 2-pt tries")),
    h("div", { class: "grid-2" },
      h("div", { class: "card" },
        h("h2", {}, "Rating swing"),
        h("p", { class: "card-sub" }, "Net Elo (offense + defense) moving between the teams, play by play. It's zero-sum, so one line: up is " + home + ", down is " + away + ". Hover for the play."),
        lineChart({
          points: swing, zero: true, symmetric: true, height: 240, legend: false, endLabels: false, ariaLabel: "Net rating swing through the game",
          series: [{ key: "swing", label: "Swing", color: "var(--ink-2)" }],
          fillZero: { above: "var(--series-1)", below: "var(--series-2)", upLabel: `${home} gaining`, downLabel: `${away} gaining` },
          yFormat: (v, step) => signed(Math.abs(v), step < 1 ? 1 : 0),
          tipRows: (p) => [[p.swing >= 0 ? "var(--series-1)" : "var(--series-2)", signed(Math.abs(p.swing), 2), `${leader(p.swing)} ahead on the swing`]],
          xMarks: (ps) => { const mk = []; ps.forEach((p, i) => { if (p.e && (i === 1 || (ps[i - 1].e && p.e.qtr !== ps[i - 1].e.qtr))) mk.push({ i, label: p.e.qtr >= 5 ? "OT" : `Q${p.e.qtr}`, major: true, minGap: 30 }); }); return mk; },
          title: (p) => (p.e ? `${ordinal(p.e.qtr)} ${p.e.time || ""} · ${p.e.att_team} ball · ${pct(p.e.p)} to win` : "Kickoff"),
          extra: (p) => (p.e ? h("div", { class: "tt-desc" }, cleanDesc(p.e.desc)) : null),
        })),
      h("div", { class: "card" }, h("h2", {}, "Ratings in → out"),
        h("div", { class: "table-wrap" }, h("table", { class: "ratings-move" },
          h("thead", {}, h("tr", {}, h("th", { class: "left" }, "Team"), h("th", {}, "Off"), h("th", {}, "Def"), h("th", {}, "Coach ⚠"))),
          h("tbody", {}, moveRow("away", away), moveRow("home", home)))),
        h("p", { class: "card-sub", style: { margin: "10px 0 0" } }, "Ratings after this game, with the change in parentheses."))),
    h("div", { class: "card" },
      h("div", { class: "chart-head" },
        h("div", {}, h("h2", {}, "Play by play"), h("p", { class: "card-sub", style: { margin: 0 } }, "Bar = each side's chance to win the play before the snap. W/T/L and Elo are from the offense's side.")),
        h("div", { class: "range" }, seg([["plays", "Plays"], ["all", "Plays + coaching"]], "plays", (v) => { showCoach = v === "all"; drawPlays(); }))),
      h("div", { class: "table-wrap" }, h("table", { class: "plays" }, tbody))),
  );
  document.title = `${away} @ ${home} · VeloCITY`;
  if (isLive) store.timers.push(setTimeout(() => render({ keepScroll: true }), 30000));
}

async function viewSeason(app, year) {
  setNav("season");
  const sm = store.summary;
  const data = await api(`/api/season/${year || sm.status.through.season}?variant=${setKey()}`);
  const all = data.teams.flatMap((t) => t.points.map((p) => p.net));
  const domain = [Math.min(0, ...all), Math.max(0, ...all)];
  const sel = h("select", { class: "select", "aria-label": "Season", onchange: (e) => go(`/season/${e.target.value}`) },
    [...data.seasons].reverse().map((y) => h("option", { value: y, selected: y === data.season ? true : null }, y)));
  const cards = data.teams.map((t, i) => h("div", { class: "multiple" },
    h("a", { class: "multiple-head", href: `/team/${t.team}?season=${data.season}`, "data-link": true },
      h("span", { class: "rank" }, i + 1), logo(t.team), h("strong", {}, t.team),
      h("span", { class: "muted" }, `${t.w}-${t.l}${t.t ? `-${t.t}` : ""}`),
      h("span", { class: "multiple-net num" }, signed(t.net))),
    lineChart({
      points: t.points, compact: true, legend: false, endLabels: false, zero: true, yDomain: domain, height: 96,
      ariaLabel: `${t.team} net rating through ${data.season}`,
      series: [{ key: "net", label: "Net", color: "var(--series-3)" }],
      yFormat: (v) => signed(v, 0), tipFormat: (v) => signed(v),
      title: (p) => `${p.week > 18 ? "Playoffs" : `Week ${p.week}`} · ${p.home ? "vs" : "@"} ${p.opponent} · ${p.points_for > p.points_against ? "W" : p.points_for < p.points_against ? "L" : "T"} ${fmt0(p.points_for)}–${fmt0(p.points_against)}`,
      onClick: (p) => go(`/game/${p.game_id}`),
    })));
  app.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, `${data.season} season`),
        h("div", { class: "sub" }, `Every team's net rating (offense + defense above average) after each game, on one shared scale. Sorted by where they finished.${store.scope === "season" ? " This season only: everyone starts at 0." : ""}`)),
      h("div", { class: "head-tools" }, sel, toggles(() => render({ keepScroll: true })))),
    h("div", { class: "multiples" }, cards));
  document.title = `${data.season} season · VeloCITY`;
}

function viewPreview(app, g) {
  app.replaceChildren(
    h("div", { class: "card", style: { marginTop: "18px" } },
      h("div", { class: "slate-meta", style: { marginBottom: "10px" } }, h("span", {}, `${g.season} week ${g.week} · kickoff ${gameStatus(g)}`)),
      h("div", { class: "scoreboard" },
        h("a", { class: "side", href: `/team/${g.away_team}`, "data-link": true }, logo(g.away_team, "lg"), h("div", { class: "tname" }, meta(g.away_team).short)),
        h("div", { class: "mid" }, "Not started"),
        h("a", { class: "side home", href: `/team/${g.home_team}`, "data-link": true }, h("div", { class: "tname" }, meta(g.home_team).short), logo(g.home_team, "lg"))),
      h("div", { style: { marginTop: "14px" } }, h("div", { class: "card-sub", style: { margin: "0 0 4px" } }, "Chance to win from today's ratings"), probBar(g.away_team, 1 - g.home_win_prob, g.home_team)),
      h("p", { class: "card-sub", style: { margin: "12px 0 0" } }, `Spread: ${g.home_win_prob >= 0.5 ? g.home_team : g.away_team} ${signed(-Math.abs(g.home_spread))}. Play-by-play chances appear here once the game kicks off.`)));
}

/* ---------- router ---------- */

function go(path) {
  history.pushState({}, "", path);
  render();
}

async function render(opts = {}) {
  store.timers.forEach(clearTimeout); store.timers = [];
  tip.hide();
  const app = $("#app");
  const url = new URL(location.href);
  const parts = url.pathname.split("/").filter(Boolean);
  const scroll = scrollY;
  try {
    if (!store.summary) await loadSummary();
    app.classList.add("loading-fade");
    if (parts[0] === "team" && parts[1]) await viewTeam(app, parts[1].toUpperCase(), url.searchParams);
    else if (parts[0] === "game" && parts[1]) await viewGame(app, parts[1]);
    else if (parts[0] === "games") await viewGames(app, url.searchParams);
    else if (parts[0] === "season") await viewSeason(app, parts[1] ? +parts[1] : null);
    else { viewRatings(app, url.searchParams); document.title = "VeloCITY"; }
    if (opts.keepScroll) scrollTo(0, scroll); else if (!opts.soft) scrollTo(0, 0);
  } catch (e) {
    if (e.building) return showBuilding(e.status);
    app.replaceChildren(h("div", { class: "empty" }, `Couldn't load this page: ${e.message}`));
  } finally {
    app.classList.remove("loading-fade");
  }
}

function showBuilding(status) {
  $("#app").replaceChildren(h("div", { class: "building" }, h("div", { class: "spinner" }),
    h("strong", {}, "Building ratings…"),
    h("span", {}, status && status.error ? status.error : "Downloading play-by-play and scoring every play since 1999. This takes a minute or two the first time.")));
  $("#status-pill").textContent = "Building…";
  setTimeout(() => render(), 5000);
}

async function loadSummary() {
  store.summary = await api("/api/summary");
  renderChrome();
}

async function poll() {
  try {
    const before = store.summary && store.summary.status;
    await loadSummary();
    const after = store.summary.status;
    const onRatings = location.pathname === "/";
    if (onRatings && before && (before.built_at !== after.built_at || before.live_at !== after.live_at)) render({ soft: true, keepScroll: true });
  } catch (e) { /* keep the last good view */ }
  const live = store.summary && store.summary.scoreboard.some((g) => g.state === "in");
  setTimeout(poll, live ? 30000 : 120000);
}

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-link]");
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey || a.target) return;
  e.preventDefault();
  go(a.getAttribute("href"));
});
window.addEventListener("popstate", () => render());
window.addEventListener("scroll", () => tip.hide(), { passive: true });
$("#team-select").addEventListener("change", (e) => { if (e.target.value) { go(`/team/${e.target.value}`); e.target.value = ""; } });
$("#theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("theme", root.dataset.theme); } catch (e) {}
});

render().then(() => setTimeout(poll, 60000));
