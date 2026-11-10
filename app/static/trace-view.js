// Draws a trace as a LangSmith-style step tree: one row per step, indented under its parent, with its time,
// a timeline bar, tokens for LLM calls and its status. Click a row for its details. Used by index.html and trace.html.
(function () {
  function el(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }

  function ms(v) {
    if (v === null || v === undefined) return "–";
    if (v < 1000) return Math.round(v) + " ms";
    return (v / 1000).toFixed(v < 10000 ? 2 : 1) + " s";
  }

  function n(v) { return Number(v || 0).toLocaleString(); }

  function tokens(s) {
    if (s.kind !== "llm") return "";
    if (s.input_tokens === null && s.output_tokens === null) return "not reported";
    return n(s.input_tokens) + " in · " + n(s.output_tokens) + " out";
  }

  function totals(trace) {
    const spans = trace.spans || [];
    const inTok = spans.reduce((a, s) => a + (s.input_tokens || 0), 0);
    const outTok = spans.reduce((a, s) => a + (s.output_tokens || 0), 0);
    const llm = spans.filter(s => s.kind === "llm");
    const unreported = llm.filter(s => s.input_tokens === null && s.output_tokens === null).length;
    return { inTok, outTok, llm: llm.length, unreported, total: trace.total_ms ?? (spans[0] && spans[0].duration_ms) };
  }

  function summaryText(trace) {
    const t = totals(trace);
    let s = ms(t.total) + " total · " + n(t.inTok + t.outTok) + " tokens";
    if (t.inTok + t.outTok) s += " (" + n(t.inTok) + " in · " + n(t.outTok) + " out)";
    s += " · " + t.llm + " LLM call" + (t.llm === 1 ? "" : "s");
    if (t.unreported) s += " (" + t.unreported + " without token counts)";
    return s;
  }

  function detailsBlock(s) {
    const box = el("div", undefined, "tv-details");
    const facts = [];
    if (s.kind === "llm") facts.push(["provider", s.provider || "unknown"], ["model", s.model || "not reported by the provider"]);
    if (s.error) facts.push(["error", s.error]);
    Object.entries(s.details || {}).forEach(([k, v]) => facts.push([k, typeof v === "object" ? JSON.stringify(v) : String(v)]));
    facts.push(["started", new Date(s.start_ts).toLocaleTimeString() + " (+" + ms(s.offset_ms) + " into the trace)"]);
    for (const [k, v] of facts) {
      const row = el("div");
      row.append(el("span", k, "tv-k"), el("span", v, "tv-v"));
      box.appendChild(row);
    }
    return box;
  }

  function render(container, trace) {
    const spans = trace.spans || [];
    const total = totals(trace).total || 1;
    const depth = {};
    for (const s of spans) depth[s.id] = s.parent_id ? (depth[s.parent_id] || 0) + 1 : 0;

    const wrap = el("div", undefined, "tv-wrap");
    const table = el("table", undefined, "tv");
    const head = el("tr");
    ["Step", "Time", "Timeline", "Tokens", "Status"].forEach(h => head.appendChild(el("th", h)));
    table.appendChild(head);

    for (const s of spans) {
      const tr = el("tr", undefined, "tv-row");
      const name = el("td", undefined, "tv-name");
      name.style.paddingLeft = (8 + depth[s.id] * 18) + "px";
      name.append(el("span", s.kind, "tv-kind tv-" + s.kind), el("span", s.name));

      const bar = el("td", undefined, "tv-bar-cell");
      const track = el("div", undefined, "tv-track");
      const fill = el("div", undefined, "tv-fill tv-fill-" + (s.status === "error" ? "error" : s.kind));
      fill.style.left = Math.min(100, (s.offset_ms / total) * 100) + "%";
      fill.style.width = Math.max(0.6, ((s.duration_ms || 0) / total) * 100) + "%";
      track.appendChild(fill);
      bar.appendChild(track);

      const status = el("td");
      status.appendChild(el("span", s.status, "tv-status tv-" + s.status));
      if (s.error) status.title = s.error;

      tr.append(name, el("td", ms(s.duration_ms), "tv-time"), bar, el("td", tokens(s), "tv-tokens"), status);
      const more = el("tr", undefined, "tv-more");
      more.hidden = true;
      const cell = el("td");
      cell.colSpan = 5;
      cell.appendChild(detailsBlock(s));
      more.appendChild(cell);
      tr.addEventListener("click", () => { more.hidden = !more.hidden; });
      table.append(tr, more);
    }
    wrap.appendChild(table);
    container.appendChild(wrap);
  }

  const css = `
    .tv-wrap { overflow-x: auto; margin-top: 10px; }
    table.tv { width: 100%; border-collapse: collapse; font-size: 13px; }
    table.tv th { text-align: left; color: #555; font-weight: 600; padding: 6px 8px; border-bottom: 1px solid #e5e5e5; white-space: nowrap; }
    table.tv td { padding: 7px 8px; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }
    tr.tv-row { cursor: pointer; }
    tr.tv-row:hover { background: #fafafa; }
    .tv-name { white-space: nowrap; }
    .tv-kind { display: inline-block; min-width: 38px; margin-right: 8px; padding: 1px 6px; border-radius: 4px; font-size: 11px;
      font-weight: 600; text-align: center; }
    .tv-chain { background: #ececec; color: #444; }
    .tv-tool { background: #e4edfb; color: #1d4f9c; }
    .tv-llm { background: #efe5fb; color: #6a2fb0; }
    .tv-time, .tv-tokens { white-space: nowrap; font-variant-numeric: tabular-nums; }
    .tv-bar-cell { width: 34%; min-width: 140px; }
    .tv-track { position: relative; height: 10px; background: #f3f3f3; border-radius: 3px; }
    .tv-fill { position: absolute; top: 0; height: 10px; border-radius: 3px; }
    .tv-fill-chain { background: #b9b9b9; }
    .tv-fill-tool { background: #6e9ee8; }
    .tv-fill-llm { background: #a37bdc; }
    .tv-fill-error { background: #d76b6b; }
    .tv-status { display: inline-block; padding: 1px 7px; border-radius: 4px; font-size: 11px; font-weight: 600; }
    .tv-ok { background: #e3f4e6; color: #17612b; }
    .tv-error { background: #fbe4e4; color: #8f2020; }
    .tv-details { padding: 6px 4px 8px; font-size: 12px; }
    .tv-details div { padding: 2px 0; }
    .tv-k { display: inline-block; min-width: 110px; color: #777; }
    .tv-v { color: #222; word-break: break-word; }
  `;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  window.TraceView = { render, summaryText, ms };
})();
