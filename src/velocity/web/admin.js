"use strict";

/* VeloCITY rules admin: edit the scoring rules and model settings, preview, save & rescore. */

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
const $ = (sel) => document.querySelector(sel);
const clone = (o) => JSON.parse(JSON.stringify(o));
const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
const DOWNS = [["1", "1st down"], ["2", "2nd down"], ["3", "3rd down"], ["4", "4th down (going for it)"]];
// [kind, dropdown label, unit after the number]
const KINDS = [
  ["share", "Gain ≥ % of yards to go", "%"],
  ["yards", "Gain ≥ yards", "yds"],
  ["short_by", "Within yards of a 1st down", "yds short"],
  ["off", "Never", ""],
];

const st = { saved: null, draft: null, defaults: null, league: { key: "nfl", decay: true }, status: null, preview: null, savedPreview: null, test: { down: 1, ydstogo: 10, yards_gained: 4, turnover: false, punt: false }, score: null };

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

/* ---------- describing a threshold in words ---------- */

function describe(t) {
  if (t.kind === "off") return "never";
  if (t.kind === "share") return t.value === 1 ? "a first down" : `${+(t.value * 100).toFixed(1)}% of the yards to go`;
  if (t.kind === "yards") return `${t.value}+ yards`;
  return t.value === 0 ? "a first down" : `ending within ${t.value} yd of a first down`;
}

/* ---------- form ---------- */

function field(label, input, hint) {
  return h("label", { class: "field" }, h("span", { class: "field-label" }, label), input, hint ? h("span", { class: "field-hint" }, hint) : null);
}

function numberInput(value, onInput, attrs = {}) {
  return h("input", { type: "number", class: "select num-input", value, step: "any", ...attrs, oninput: (e) => onInput(e.target.value === "" ? NaN : +e.target.value) });
}

function thresholdEditor(down, which) {
  const t = st.draft.rules.downs[down][which];
  const wrap = h("div", { class: "threshold" });
  const kindSel = h("select", { class: "select", "aria-label": `${down} ${which} rule`, onchange: (e) => {
    t.kind = e.target.value;
    t.value = t.kind === "share" ? 0.5 : t.kind === "yards" ? 3 : t.kind === "short_by" ? 1 : 0;
    changed(true);
  } }, KINDS.map(([k, label]) => h("option", { value: k, selected: k === t.kind ? true : null }, label)));
  wrap.append(kindSel);
  if (t.kind !== "off") {
    const shown = t.kind === "share" ? +(t.value * 100).toFixed(2) : t.value;
    wrap.append(numberInput(shown, (v) => { t.value = t.kind === "share" ? v / 100 : v; changed(false); },
      { min: 0, max: t.kind === "share" ? 300 : 99, "aria-label": `${down} ${which} value` }),
    h("span", { class: "unit" }, KINDS.find((k) => k[0] === t.kind)[2]));
  }
  return wrap;
}

function renderEditor() {
  const r = st.draft.rules, m = st.draft.model, d = st.defaults.model;
  const form = $("#editor");
  form.replaceChildren(
    h("section", { class: "card" },
      h("h2", {}, "Play scoring"),
      h("p", { class: "card-sub" }, "Every run, pass and punt is a one-play game: the offense wins (1), ties (½) or loses (0). A tie is only checked when the play didn't win. A first down is 100% of the yards to go."),
      h("div", { class: "table-wrap" }, h("table", { class: "rules-table" },
        h("thead", {}, h("tr", {}, h("th", { class: "left" }, "Down"), h("th", { class: "left" }, "Offense wins if it…"), h("th", { class: "left" }, "Tie if it…"))),
        h("tbody", {}, DOWNS.map(([d, label]) => h("tr", {},
          h("td", { class: "left" }, h("strong", {}, label)),
          h("td", { class: "left" }, thresholdEditor(d, "win")),
          h("td", { class: "left" }, thresholdEditor(d, "tie"))))))),
      h("div", { class: "field-row" },
        field("Punts count as", h("select", { class: "select", onchange: (e) => { r.punt = e.target.value; changed(false); } },
          [["win", "a win for the offense"], ["tie", "a tie"], ["loss", "a loss for the offense"], ["exclude", "not counted"]].map(([v, l]) => h("option", { value: v, selected: v === r.punt ? true : null }, l)))),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: r.turnover_loss ? true : null, onchange: (e) => { r.turnover_loss = e.target.checked; changed(false); } }),
          h("span", {}, "Turnovers are always a loss", h("span", { class: "field-hint" }, "Off: an interception or lost fumble is scored by the yards gained before it."))),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: r.field_goals ? true : null, onchange: (e) => { r.field_goals = e.target.checked; changed(false); } }),
          h("span", {}, "Field goals count", h("span", { class: "field-hint" }, "Made = offense win; missed or blocked = defense win."))))),

    h("section", { class: "card" },
      h("h2", {}, "Weights"),
      h("p", { class: "card-sub" }, "How much more a play moves the ratings, by the situation before the snap (so it never favors either side's result)."),
      h("div", { class: "field-row" },
        field("Red zone (inside the 20)", numberInput(r.weights.red_zone, (v) => { r.weights.red_zone = v; changed(false); }, { min: 0.1, max: 10 }), "Default 1.5×"),
        field("Goal to go", numberInput(r.weights.goal_to_go, (v) => { r.weights.goal_to_go = v; changed(false); }, { min: 0.1, max: 10 }), "Replaces the red-zone weight · default 2×"),
        field("Field-goal attempts", numberInput(r.weights.field_goal, (v) => { r.weights.field_goal = v; changed(false); }, { min: 0.1, max: 10 }), "Default 1.5×"))),

    h("section", { class: "card" },
      h("h2", {}, "V-City"),
      h("p", { class: "card-sub" }, "Volatile Chunks & Impressive Turnovers, Y'know: the second axis. Every run and pass also scores a big play for the offense and havoc for the defense, from 0 to 1."),
      h("h3", { class: "mini-h" }, "Big plays (offense)"),
      h("div", { class: "field-row" },
        field("Credit starts at (yards)", numberInput(r.boom.start, (v) => { r.boom.start = v; changed(false); }, { min: 0, max: 99 })),
        field("Full credit at (yards)", numberInput(r.boom.full, (v) => { r.boom.full = v; changed(false); }, { min: 1, max: 99 })),
        field("TD bonus from outside the red zone", numberInput(r.boom.td_bonus, (v) => { r.boom.td_bonus = v; changed(false); }, { min: 0, max: 1 }), "Added to the credit, capped at 1")),
      h("h3", { class: "mini-h" }, "Havoc (defense)"),
      h("div", { class: "field-row" },
        field("Sack", numberInput(r.havoc.sack_base, (v) => { r.havoc.sack_base = v; changed(false); }, { min: 0, max: 1 })),
        field("+ per yard lost", numberInput(r.havoc.sack_per_yard, (v) => { r.havoc.sack_per_yard = v; changed(false); }, { min: 0, max: 1 })),
        field("Tackle for loss", numberInput(r.havoc.tfl_base, (v) => { r.havoc.tfl_base = v; changed(false); }, { min: 0, max: 1 })),
        field("+ per yard lost", numberInput(r.havoc.tfl_per_yard, (v) => { r.havoc.tfl_per_yard = v; changed(false); }, { min: 0, max: 1 })),
        field("× on 3rd / 4th down", numberInput(r.havoc.late_down, (v) => { r.havoc.late_down = v; changed(false); }, { min: 0.5, max: 3 }))),
      h("div", { class: "field-row" },
        field("Interception", numberInput(r.havoc.interception, (v) => { r.havoc.interception = v; changed(false); }, { min: 0, max: 1 })),
        field("Lost fumble", numberInput(r.havoc.fumble, (v) => { r.havoc.fumble = v; changed(false); }, { min: 0, max: 1 })),
        field("+ forced in the backfield", numberInput(r.havoc.backfield, (v) => { r.havoc.backfield = v; changed(false); }, { min: 0, max: 1 })),
        field("+ per return yard", numberInput(r.havoc.return_per_yard, (v) => { r.havoc.return_per_yard = v; changed(false); }, { min: 0, max: 0.1 })),
        field("Return TD", numberInput(r.havoc.return_td, (v) => { r.havoc.return_td = v; changed(false); }, { min: 0, max: 1 })),
        field("Safety", numberInput(r.havoc.safety, (v) => { r.havoc.safety = v; changed(false); }, { min: 0, max: 1 })))),

    h("section", { class: "card" },
      h("h2", {}, "Coaching staff ⚠"),
      h("p", { class: "card-sub" }, "These go to the coaching-staff rating instead of offense/defense. Disclaimer: it will suck."),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: r.penalties ? true : null, onchange: (e) => { r.penalties = e.target.checked; changed(false); } }),
        h("span", {}, "Accepted penalties", h("span", { class: "field-hint" }, "The flagged team's staff loses to the other staff."))),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: r.timeouts ? true : null, onchange: (e) => { r.timeouts = e.target.checked; changed(true); } }),
        h("span", {}, "Timeouts", h("span", { class: "field-hint" }, "The staff that calls a charged timeout loses, unless it's late in the half."))),
      r.timeouts ? field("…when more than this many seconds are left in the half", numberInput(r.timeout_seconds, (v) => { r.timeout_seconds = v; changed(false); }, { min: 0, max: 1800, step: 1 }), "120 = outside the two-minute warning") : null,
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: r.two_point ? true : null, onchange: (e) => { r.two_point = e.target.checked; changed(false); } }),
        h("span", {}, "Two-point tries", h("span", { class: "field-hint" }, "Offense staff wins on a conversion, defense staff on a stop.")))),

    h("section", { class: "card" },
      h("h2", {}, "Model"),
      h("p", { class: "card-sub" }, "How far ratings move. Higher K reacts faster but chases noise; above ~3 it predicts worse than no ratings at all."),
      h("div", { class: "field-row" },
        field("K per play", numberInput(m.k, (v) => { m.k = v; changed(false); }, { min: 0.01, max: 20 }), `Default ${d.k}`),
        field("K per coaching event", numberInput(m.k_coach, (v) => { m.k_coach = v; changed(false); }, { min: 0, max: 20 }), `Default ${d.k_coach}`)),
      h("div", { class: "field-row" },
        field("Off-season pull toward average, O/D (%)", numberInput(+(m.season_regression * 100).toFixed(1), (v) => { m.season_regression = v / 100; changed(false); }, { min: 0, max: 100 }), "Seasons without roster data, or with decay off"),
        field("Off-season pull, coaching (%)", numberInput(+(m.coach_regression * 100).toFixed(1), (v) => { m.coach_regression = v / 100; changed(false); }, { min: 0, max: 100 }))),
      h("div", { class: "field-row" },
        field("K per V-City play", numberInput(m.k_vcity, (v) => { m.k_vcity = v; changed(false); }, { min: 0, max: 20 }), `Default ${d.k_vcity}`),
        field("Off-season pull, V-City (%)", numberInput(+(m.vcity_regression * 100).toFixed(1), (v) => { m.vcity_regression = v / 100; changed(false); }, { min: 0, max: 100 }), `Default ${Math.round(d.vcity_regression * 100)}%`)),
      st.league.decay ? h("label", { class: "check" }, h("input", { type: "checkbox", checked: m.decay ? true : null, onchange: (e) => { m.decay = e.target.checked; changed(false); } }),
        h("span", {}, "Team-specific off-season decay", h("span", { class: "field-hint" }, "Returning snaps, lineup age and head-coach changes set each team's pull (2014 on)."))) : null,
      h("div", { class: "field-row" },
        field("Garbage time: offense win chance below (%)", numberInput(+(m.garbage_wp[0] * 100).toFixed(1), (v) => { m.garbage_wp[0] = v / 100; changed(false); }, { min: 0, max: 100 })),
        field("…or above (%)", numberInput(+(m.garbage_wp[1] * 100).toFixed(1), (v) => { m.garbage_wp[1] = v / 100; changed(false); }, { min: 0, max: 100 })))),

    h("div", { class: "admin-actions" },
      h("button", { type: "button", class: "btn ghost", onclick: () => { st.draft = clone(st.defaults); changed(true); } }, "Reset everything to defaults")),
  );
}

/* ---------- side panel: try a play + season preview ---------- */

function renderSide() {
  const t = st.test;
  const res = st.score == null ? null : st.score === 1 ? ["W", "Offense wins"] : st.score === 0.5 ? ["T", "Tie"] : ["L", "Offense loses"];
  const pv = st.preview;
  const saved = st.savedPreview ? Object.fromEntries(st.savedPreview.downs.map((d) => [d.key, d])) : {};
  const delta = (d, k) => {
    const s = saved[d.key];
    if (!s) return null;
    const diff = Math.round((d[k] - s[k]) * 100);
    return diff ? h("span", { class: diff > 0 ? "delta-up" : "delta-down" }, ` ${diff > 0 ? "+" : "−"}${Math.abs(diff)}`) : null;
  };
  $("#side").replaceChildren(
    h("section", { class: "card sticky" },
      h("h2", {}, "Try a play"),
      h("div", { class: "field-row tight" },
        field("Down", h("select", { class: "select", disabled: t.punt ? true : null, onchange: (e) => { t.down = +e.target.value; testPlay(); } },
          [1, 2, 3, 4].map((d) => h("option", { value: d, selected: d === t.down ? true : null }, ["1st", "2nd", "3rd", "4th"][d - 1])))),
        field("Yards to go", numberInput(t.ydstogo, (v) => { t.ydstogo = v; testPlay(); }, { min: 1, max: 99, disabled: t.punt ? true : null })),
        field("Yards gained", numberInput(t.yards_gained, (v) => { t.yards_gained = v; testPlay(); }, { min: -99, max: 99, disabled: t.punt ? true : null }))),
      h("div", { class: "field-row tight" },
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: t.turnover ? true : null, onchange: (e) => { t.turnover = e.target.checked; testPlay(); } }), "Turnover"),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: t.punt ? true : null, onchange: (e) => { t.punt = e.target.checked; renderSide(); testPlay(); } }), "Punt")),
      res ? h("div", { class: "try-result" }, h("span", { class: `res-badge big res-${res[0]}` }, res[0]), h("span", {}, res[1])) : null,

      h("h2", { style: { marginTop: "18px" } }, pv ? `${pv.season} season under these rules` : "Season preview"),
      h("p", { class: "card-sub" }, "Share of real plays the offense would win, tie and lose. Small numbers show the change from the saved rules (points)."),
      pv ? h("div", { class: "dist" }, pv.downs.map((d) => h("div", { class: "dist-row" },
        h("div", { class: "dist-head" }, h("span", {}, d.label), h("span", { class: "muted" }, `${d.plays.toLocaleString()} plays`)),
        h("div", { class: "dist-bar", role: "img", "aria-label": `${d.label}: win ${pct(d.win)}, tie ${pct(d.tie)}, loss ${pct(d.loss)}` },
          ...[["win", "var(--good)"], ["tie", "var(--tie)"], ["loss", "var(--bad)"]].filter(([k]) => d[k] > 0).map(([k, c]) => h("span", { style: { flex: d[k], background: c } }))),
        h("div", { class: "dist-labels" },
          h("span", {}, h("b", {}, "W "), pct(d.win), delta(d, "win")),
          h("span", {}, h("b", {}, "T "), pct(d.tie), delta(d, "tie")),
          h("span", {}, h("b", {}, "L "), pct(d.loss), delta(d, "loss")))))) : h("div", { class: "muted" }, "Loading…"),
      pv ? h("p", { class: "card-sub", style: { marginTop: "10px" } },
        `Coaching events per season: ${["penalty", "timeout", "two_point"].map((k) => `${(pv.coaching[k] || 0).toLocaleString()} ${{ penalty: "penalties", timeout: "timeouts", two_point: "2-pt tries" }[k]}`).join(", ")}.`) : null,
      pv && pv.vcity ? h("p", { class: "card-sub" },
        `V-City per team per game: ${pv.vcity.boom_per_game.toFixed(2)} big-play credit (${pv.vcity.full_booms_per_game.toFixed(2)} full-credit booms), ${pv.vcity.havoc_per_game.toFixed(2)} havoc. ${Math.round(pv.vcity.weighted_share * 100)}% of plays are weighted.`) : null),
  );
}

/* ---------- state changes ---------- */

let previewTimer = null;
function changed(rerender) {
  const dirty = JSON.stringify(st.draft) !== JSON.stringify(st.saved);
  $("#savebar").hidden = !dirty;
  $("#save-error").textContent = "";
  if (rerender) renderEditor();
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 250);
}

async function runPreview() {
  try {
    const out = await api("/api/preview", { rules: st.draft.rules, plays: [st.test] });
    st.preview = out.preview; st.score = out.scores[0];
    $("#save-error").textContent = "";
  } catch (e) {
    $("#save-error").textContent = e.message;
  }
  renderSide();
}

async function testPlay() {
  try {
    const out = await api("/api/preview", { rules: st.draft.rules, plays: [st.test] });
    st.score = out.scores[0];
  } catch (e) { st.score = null; }
  renderSide();
}

function renderStatus() {
  const s = st.status || {};
  const pill = $("#status-pill");
  if (s.building) pill.replaceChildren(h("span", { class: "spinner small" }), "Rescoring every season…");
  else if (s.error) pill.textContent = s.error;
  else pill.textContent = s.built_at ? `Ratings built ${new Date(s.built_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "Not built yet";
}

async function pollStatus() {
  try { st.status = await api("/api/status"); } catch (e) { /* keep last */ }
  renderStatus();
  setTimeout(pollStatus, st.status && st.status.building ? 2000 : 15000);
}

async function save() {
  const btn = $("#save");
  btn.disabled = true;
  try {
    const out = await api("/api/settings", st.draft);
    st.saved = clone(out.settings); st.draft = clone(out.settings); st.status = out.status;
    st.savedPreview = st.preview;
    $("#savebar").hidden = true;
    renderEditor(); renderStatus();
    setTimeout(pollStatus, 1500);
  } catch (e) {
    $("#save-error").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function init() {
  const out = await api("/api/settings");
  st.saved = clone(out.settings); st.draft = clone(out.settings); st.defaults = out.defaults; st.status = out.status;
  st.league = out.league || { key: "nfl", name: "NFL", decay: true };
  if (st.league.key !== "nfl") document.title = `VeloCITY ${st.league.name} admin`;
  if (out.public_port) {
    const link = $("#site-link");
    link.href = `${location.protocol}//${location.hostname}:${out.public_port}/`;
    link.hidden = false;
  }
  renderEditor(); renderSide(); renderStatus();
  const first = await api("/api/preview", { rules: st.saved.rules, plays: [st.test] }).catch(() => null);
  if (first) { st.preview = first.preview; st.savedPreview = first.preview; st.score = first.scores[0]; }
  renderSide();
  setTimeout(pollStatus, 15000);
}

$("#save").addEventListener("click", save);
$("#discard").addEventListener("click", () => { st.draft = clone(st.saved); changed(true); });
window.addEventListener("beforeunload", (e) => { if (!$("#savebar").hidden) { e.preventDefault(); e.returnValue = ""; } });
init().catch((e) => { $("#editor").replaceChildren(h("div", { class: "empty" }, `Couldn't load settings: ${e.message}`)); });
