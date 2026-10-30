// factory dashboard (F-31) — the run view's live renderer.
//
// Reads the trace events the server embeds in the page, draws the phase
// swim lanes on a time axis, then keeps the run live over the
// server-sent event stream (GET /runs/<run-id>/events). If the stream
// drops (a proxy timeout, a sleeping machine), the page falls back to
// polling a full page reload — the events are re-embedded, the lanes
// re-render. No framework.

"use strict";

function text(node, value) {
  node.textContent = value == null ? "" : String(value);
}

function el(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content != null) text(node, content);
  return node;
}

// One trace event -> one lane row (tool calls and gate checks only;
// the phase bookkeeping builds the lanes themselves).
function laneRow(event) {
  const row = el("li", event.ok ? "tool" : "tool failed");
  if (event.type === "tool_call") {
    row.className = event.ok ? "tool" : "tool failed";
    row.append(el("code", "", event.tool), el("span", "", " "));
    row.append(el("span", "", event.args || ""));
    row.append(el("span", "", " → " + (event.result || "")));
  } else if (event.type === "gate_check") {
    row.className = event.ok ? "gate" : "gate failed";
    row.append(el("span", "",
      "gate check [" + (event.layer || "?") + "] " +
      (event.item || "?") + ": " + (event.note || "")));
  } else if (event.type === "session_end") {
    row.className = "session";
    const parts = [];
    if (event.tokens) {
      parts.push("tokens " + (event.tokens.input_tokens || 0) +
        " in / " + (event.tokens.output_tokens || 0) + " out");
    }
    if (event.cost_usd != null) parts.push("cost $" + Number(event.cost_usd).toFixed(4));
    row.append(el("span", "", parts.join(" · ")));
  } else {
    return null;
  }
  return row;
}

function spendLine(tokens, costUsd) {
  const parts = [];
  if (tokens) {
    parts.push("tokens " + (tokens.input_tokens || 0) + " in / " +
      (tokens.output_tokens || 0) + " out");
  }
  if (costUsd != null) parts.push("cost $" + Number(costUsd).toFixed(4));
  return parts.join(" · ");
}

// All events -> one phase per swim lane, on a shared time axis. The
// axis is the page-width percentage each event lands at, from the
// first event's timestamp to the last.
function renderLanes(container, events) {
  container.replaceChildren();
  const stamps = events.map(e => Date.parse(e.ts)).filter(Number.isFinite);
  const start = Math.min.apply(null, stamps);
  const end = Math.max.apply(null, stamps);
  const span = end > start ? end - start : 1;

  let lane = null;
  let list = null;
  let cost = null;
  const position = event => {
    const at = Date.parse(event.ts);
    return Number.isFinite(at) ? ((at - start) / span) * 100 : null;
  };

  for (const event of events) {
    if (event.type === "phase_start") {
      lane = el("div", "lane");
      lane.dataset.phase = event.phase || "?";
      lane.append(el("h3", "", event.phase || "?"));
      const axis = el("div", "axis");
      const mark = el("span", "mark");
      const at = position(event);
      if (at != null) mark.style.marginLeft = at + "%";
      axis.append(mark);
      lane.append(axis);
      list = el("ul");
      lane.append(list);
      container.append(lane);
    }
    if (!lane) continue;
    if (event.type === "phase_end") {
      lane.dataset.tokens = JSON.stringify(event.tokens || null);
      const spend = spendLine(event.tokens, cost);
      if (spend) lane.append(el("p", "spend", spend));
    } else if (event.type === "session_end") {
      cost = event.cost_usd != null ? event.cost_usd : cost;
    } else {
      const row = laneRow(event);
      if (row && list) list.append(row);
    }
  }
}

function main() {
  const container = document.getElementById("lanes");
  const state = document.getElementById("stream-state");
  const embedded = document.getElementById("trace-events");
  const runId = (document.getElementById("run") || {}).dataset
    ? document.getElementById("run").dataset.runId : null;
  if (!container || !embedded) return;
  const events = JSON.parse(embedded.textContent);
  renderLanes(container, events);
  if (!runId || typeof EventSource === "undefined") return;

  // Live: the same events, appended as the run writes them.
  const source = new EventSource("/runs/" + encodeURIComponent(runId) + "/events");
  let live = true;
  source.onopen = () => {
    if (state) { state.textContent = "live"; state.className = "live"; }
  };
  source.onmessage = message => {
    if (!live) {        // a straggler frame after fallback: ignore
      return;
    }
    try {
      const event = JSON.parse(message.data);
      if (event.type === "run_end") {
        source.close();
        if (state) { state.textContent = "ended: " + (event.outcome || "?"); }
        return;
      }
      events.push(event);
      renderLanes(container, events);
    } catch (err) { /* a malformed frame: keep the lanes we have */ }
  };
  // The stream dropped: fall back to polling. A reload re-fetches the
  // page, whose embedded events carry everything the stream delivered.
  source.onerror = () => {
    if (!live) return;
    live = false;
    source.close();
    if (state) {
      state.textContent = "stream lost — polling";
      state.className = "polling";
    }
    const url = new URL(window.location.href);
    url.searchParams.set("poll", String(Date.now()));
    window.setTimeout(() => window.location.replace(url), 5000);
  };
}

main();
