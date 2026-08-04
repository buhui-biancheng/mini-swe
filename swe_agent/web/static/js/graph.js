// waku dashboard — graph workflows: the topology chart + its live animation.
// Split out: classic <script>, shared global scope. Load order: static/README.md.
//
// The chart is DATA-DRIVEN: it renders Graph.describe() served in /api/data
// (d.graph.workflows), so the picture is provably the topology the engine
// runs — the anti-drift lesson learned from archSVG's byte-freeze. Never
// hand-edit a workflow's shape here; change the workflow and this follows.
// Ids are namespaced "g-" so they can never collide with archSVG's ids.

// --- force-directed layout: Obsidian-style scatter (cached so it doesn't jump
// on every 5s refresh). Deterministic circle seed, no randomness. Bounded by a
// hard box so the chart always fits the panel. Tuned for ≤~80 nodes (the
// backend topology cap); larger graphs degrade into a dense dot cloud.
let CODE_FOCUS = null;   // 当前被 AI 读取的节点（其标签常显，跨渲染保持）
const _zoomState = {};   // 每张图的缩放/平移（viewBox），跨 5s 重渲染保持
const _zoomBase = {};    // 每张图的自然尺寸（首次交互时记录，用于按缩放比钳制）
const FORCE = { baseRep: 8000, power: 1.3, rest: 60, spring: 0.06,
                gravity: 0.006, damping: 0.55, vmax: 25, iters: 350 };
let _codeLayoutCache = null, _codeLayoutSig = "";
function graphForceLayout(wf){
  const sig = wf.nodes.length + "|" + wf.edges.length + "|" + wf.nodes.map(n => n.name).join(",");
  if (_codeLayoutCache && _codeLayoutSig === sig) return _codeLayoutCache;
  const names = wf.nodes.map(n => n.name), n = names.length;
  const CW = 1000, CH = 560, M = 10;
  const K = FORCE.baseRep * Math.pow(n, FORCE.power), G = FORCE.gravity * n;
  const pos = {}, vel = {};
  names.forEach((nm, i) => {
    const a = (i / n) * Math.PI * 2;
    pos[nm] = { x: CW / 2 + Math.sqrt(n) * 50 * Math.cos(a), y: CH / 2 + Math.sqrt(n) * 50 * Math.sin(a) };
    vel[nm] = { x: 0, y: 0 };
  });
  const iters = n > 150 ? 100 : FORCE.iters;   // 大图降迭代，避免首次布局过慢
  for (let it = 0; it < iters; it++){
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++){
      const a = names[i], b = names[j];
      let dx = pos[a].x - pos[b].x, dy = pos[a].y - pos[b].y;
      let d2 = dx * dx + dy * dy; if (d2 < 1){ dx = 1; dy = 0; d2 = 1; }
      const d = Math.sqrt(d2), f = K / d2, fx = f * dx / d, fy = f * dy / d;
      vel[a].x += fx; vel[a].y += fy; vel[b].x -= fx; vel[b].y -= fy;
    }
    wf.edges.forEach(e => {
      const a = pos[e.src], b = pos[e.dst]; if (!a || !b) return;
      const dx = b.x - a.x, dy = b.y - a.y, d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const f = FORCE.spring * (d - FORCE.rest), fx = f * dx / d, fy = f * dy / d;
      vel[e.src].x += fx; vel[e.src].y += fy; vel[e.dst].x -= fx; vel[e.dst].y -= fy;
    });
    names.forEach(nm => {
      vel[nm].x += G * (CW / 2 - pos[nm].x); vel[nm].y += G * (CH / 2 - pos[nm].y);
      vel[nm].x *= FORCE.damping; vel[nm].y *= FORCE.damping;
      const v = Math.hypot(vel[nm].x, vel[nm].y);
      if (v > FORCE.vmax){ vel[nm].x *= FORCE.vmax / v; vel[nm].y *= FORCE.vmax / v; }
      pos[nm].x += vel[nm].x; pos[nm].y += vel[nm].y;
      if (pos[nm].x < M){ pos[nm].x = M; vel[nm].x = Math.abs(vel[nm].x) * 0.5; }
      if (pos[nm].x > CW - M){ pos[nm].x = CW - M; vel[nm].x = -Math.abs(vel[nm].x) * 0.5; }
      if (pos[nm].y < M){ pos[nm].y = M; vel[nm].y = Math.abs(vel[nm].y) * 0.5; }
      if (pos[nm].y > CH - M){ pos[nm].y = CH - M; vel[nm].y = -Math.abs(vel[nm].y) * 0.5; }
    });
  }
  // 拥挤判定：最小节点间距 < 55px 时标签会重叠 → 截断显示
  let minD = Infinity;
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++){
    const d = Math.hypot(pos[names[i]].x - pos[names[j]].x, pos[names[i]].y - pos[names[j]].y);
    if (d < minD) minD = d;
  }
  _codeLayoutCache = { pos, W: CW, H: CH, crowded: minD < 55 };
  _codeLayoutSig = sig;
  return _codeLayoutCache;
}

// node_id → 显示短名：取 :: 后段（函数/类），文件节点取 basename
function _shortName(id){
  const i = id.lastIndexOf("::");
  return i >= 0 ? id.slice(i + 2) : id.split("/").pop();
}

// 散点 + 有向边（Obsidian 式）：「焦点+上下文」。全图做淡背景小点，
// 当前被读取的节点（CODE_FOCUS）+ 其 1 跳邻居放大带标签，其余淡出。
// 任何规模不砍节点——砍了 AI 读到被砍节点就不亮灯。箭头复用 fsm.js 的 #arr。
function graphSVG(wf, opts = {}){
  const { pos, W: CW, H: CH, crowded } = graphForceLayout(wf);
  const kinds = Object.fromEntries(wf.nodes.map(n => [n.name, n.kind]));
  const nid = n => `g-${wf.name}-${n}`;
  // 焦点集：CODE_FOCUS + 1 跳邻居（从边数据算，不手画）
  const focusSet = new Set();
  if (CODE_FOCUS){
    focusSet.add(CODE_FOCUS);
    wf.edges.forEach(e => {
      if (e.src === CODE_FOCUS) focusSet.add(e.dst);
      if (e.dst === CODE_FOCUS) focusSet.add(e.src);
    });
  }
  const nodeDot = n => {
    const p = pos[n], kind = kinds[n] || "function";
    const inFocus = focusSet.has(n);
    const mode = focusSet.size ? (inFocus ? "focus" : "ctx") : "";
    const r = inFocus ? 6 : (focusSet.size ? 2.5 : 4);
    const short = _shortName(n);
    const label = crowded ? short.slice(0, 6) + "…" : short;   // 挤则截断，悬停看全称
    return `<g class="gnode ${mode}" data-node="${nid(n)}" data-kind="${kind}">
      <circle class="gdot" cx="${p.x}" cy="${p.y}" r="${r}"/>
      <title>${esc(n)}</title>
      <text class="glabel" x="${p.x}" y="${p.y - 11}" text-anchor="middle">${esc(label)}</text>
      ${crowded ? `<text class="glabel hover" x="${p.x}" y="${p.y - 11}" text-anchor="middle">${esc(short)}</text>` : ""}
    </g>`;
  };
  const edgeLine = e => {
    const a = pos[e.src], b = pos[e.dst]; if (!a || !b) return "";
    const touched = focusSet.size ? (focusSet.has(e.src) || focusSet.has(e.dst)) : true;
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
    const ux = dx / d, uy = dy / d, R = 6;
    const x1 = a.x + ux * R, y1 = a.y + uy * R;
    const x2 = b.x - ux * (R + 8), y2 = b.y - uy * (R + 8);
    return `<path class="flow${touched ? "" : " ctx"}" data-edge="g-${wf.name}-${e.src}-${e.dst}" d="M${x1} ${y1} L${x2} ${y2}"/>`;
  };
  const zoom = _zoomState[wf.name] || {x: 0, y: 0, w: CW, h: CH};   // 记住用户缩放
  return `<div style="overflow-x:auto"><svg viewBox="${zoom.x} ${zoom.y} ${zoom.w} ${zoom.h}" class="arch graphchart" data-zoomkey="${wf.name}"
      style="max-width:${CW}px" role="img">
    ${wf.edges.map(edgeLine).join("")}
    ${wf.nodes.map(n => nodeDot(n.name)).join("")}
  </svg></div>`;
}

// ---- 滚轮缩放 + 拖拽平移（事件委托：5s 重渲染重建 DOM 后依然生效）----
// 缩放状态存 _zoomState[key]，渲染时从它取 viewBox，跨刷新保持。
(function(){
  const ZOOM_MIN = 0.08, ZOOM_MAX = 2.5, ZF = 1.1;   // 最小/最大缩放、每格倍率
  let pan = null;   // 拖拽状态
  const svgFrom = t => (t && t.closest ? t.closest("svg.arch") : null);
  function parseVB(svg){
    const p = (svg.getAttribute("viewBox") || "0 0 100 100").trim().split(/[\s,]+/).map(Number);
    return {x: p[0] || 0, y: p[1] || 0, w: p[2] || 100, h: p[3] || 100};
  }
  function applyVB(svg, vb, key){
    svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
    if (key) _zoomState[key] = {x: vb.x, y: vb.y, w: vb.w, h: vb.h};
  }
  document.addEventListener("wheel", e => {
    const svg = svgFrom(e.target);
    if (!svg) return;
    e.preventDefault(); e.stopPropagation();   // 图上滚轮 = 缩放，不滚页面
    const key = svg.dataset.zoomkey;
    const vb = parseVB(svg);
    if (key && !_zoomBase[key]) _zoomBase[key] = {w: vb.w, h: vb.h};   // 自然尺寸
    const base = (key && _zoomBase[key]) || {w: vb.w, h: vb.h};
    const f = e.deltaY < 0 ? 1 / ZF : ZF;     // 上滚放大 / 下滚缩小
    // 按「缩放比」钳制（不是绝对宽度）：比例在 [ZOOM_MIN, ZOOM_MAX] 内
    const scale = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, (vb.w / base.w) * f));
    const nw = base.w * scale, nh = base.h * scale;
    const r = svg.getBoundingClientRect();
    const u = (e.clientX - r.left) / r.width, v = (e.clientY - r.top) / r.height;
    const ux = vb.x + u * vb.w, uy = vb.y + v * vb.h;   // 光标下坐标点保持不动
    vb.x = ux - u * nw; vb.y = uy - v * nh; vb.w = nw; vb.h = nh;
    applyVB(svg, vb, key);
  }, {passive: false});
  document.addEventListener("mousedown", e => {
    const svg = svgFrom(e.target);
    if (!svg) return;
    e.preventDefault();
    const vb = parseVB(svg), r = svg.getBoundingClientRect();
    pan = {svg, key: svg.dataset.zoomkey, startX: e.clientX, startY: e.clientY,
           vx: vb.x, vy: vb.y, vw: vb.w, vh: vb.h, cw: r.width, ch: r.height};
    svg.classList.add("panning");
  });
  document.addEventListener("mousemove", e => {
    if (!pan) return;
    applyVB(pan.svg, {
      x: pan.vx - (e.clientX - pan.startX) * (pan.vw / pan.cw),
      y: pan.vy - (e.clientY - pan.startY) * (pan.vh / pan.ch),
      w: pan.vw, h: pan.vh,
    }, pan.key);
  });
  document.addEventListener("mouseup", () => {
    if (pan){ pan.svg.classList.remove("panning"); pan = null; }
  });
})();

// Which workflow is running RIGHT NOW, set by graph_start and cleared by
// graph_end. The Overview panel used to read the last COMPLETED run, so during
// a gather it still showed triage — and since animateGraphStage only lights a
// chart that is on screen, nothing lit up either. One wrong chart caused both
// bugs: you saw the old shape, and you saw it stay dark.
let GRAPH_LIVE = null;
// The workflow to keep showing once a run ENDS. Without it the panel fell back
// to d.graph.runs[0] the instant graph_end fired — and that payload is only
// refreshed by the /api/data poll, so for one beat it still named the PREVIOUS
// run. Observed as: swap to gather, flick back to triage, then back to gather
// when the poll caught up. Remembering locally means the panel never shows a
// workflow older than the one it just watched.
let GRAPH_SHOWN = null;

// --- the compact Overview panel: the harness auto-decides, this reflects it.
function graphPanel(d){
  const g = d.graph || {enabled: false, workflows: [], runs: [], stats: {quick: 0, full: 0}};
  // Overview is a STATUS surface — "what just happened" — while the Graph tab
  // is a reference one: "what shapes exist". So this shows the workflow that
  // most recently RAN, from the trace. Pinning it to workflows[0] meant Overview
  // showed triage forever, seconds after a gather, which is why the panel read
  // as leftovers rather than as news.
  // A run in flight wins over the last finished one — during a gather you want
  // to watch the gather, not read about the triage that came before it.
  const showing = GRAPH_LIVE || GRAPH_SHOWN || ((g.runs || [])[0] || {}).workflow;
  const last = (g.runs || [])[0];
  const wf = (g.workflows || []).find(w => w && w.name === showing)
             || (g.workflows || [])[0];
  const tot = g.stats.quick + g.stats.full;
  const seg = (cls, n, label, pct) =>
    `<div class="${cls}" style="width:${pct}%">${pct >= 14 ? `${n} ${label}` : ""}</div>`;
  const split = !tot
    ? `<div class="meta" style="margin:6px 0 10px">no graph turns yet — every message will route here once it's on</div>`
    : `<div class="splitbar">
        ${seg("seg-skip", g.stats.quick, "quick", Math.round(g.stats.quick / tot * 100))}
        ${seg("seg-ret", g.stats.full, "full", 100 - Math.round(g.stats.quick / tot * 100))}
      </div><div class="meta" style="margin:6px 0 10px">${g.stats.quick} answered by the small model alone — the loop never woke</div>`;
  // The flag gates TRIAGE — the per-message door — and nothing else. `waku
  // gather` is a routine you start yourself and runs regardless, so the old
  // copy ("off = every turn runs the classic loop") was quietly false the
  // moment a second workflow existed.
  if (!g.enabled && !last)
    return `<div class="card"><div class="meta">The per-message graph door is <b>off</b> — every chat turn
      runs the classic loop above. Switch on <b>graph workflows</b> in
      <a class="reveal" onclick="location.hash='settings'">Settings</a> to triage each message first.
      Workflows you run yourself, like <code>make gather</code>, do not need the flag —
      <a class="reveal" onclick="location.hash='graph'">see them here</a>.</div></div>`;
  const when = GRAPH_LIVE
    ? `<span class="live-dot"></span><b>${esc(GRAPH_LIVE)}</b> running now`
    : last
    ? `last run: <b>${esc(last.workflow || "")}</b>${last.ms ? ` · ${(last.ms/1000).toFixed(1)}s` : ""}${
        last.steps ? ` · ${last.steps} nodes` : ""}`
    : "live — nodes light up as a turn flows through";
  return `<div class="card" style="cursor:pointer" onclick="location.hash='graph'">
    ${g.enabled ? split : ""}${wf ? graphSVG(wf) : ""}
    <div class="meta" style="margin-top:8px">${when} · click for the full story</div></div>`;
}

// --- live animation: same machinery as the loop's STAGE map. hot() lights
// every copy on the page, so the Overview panel and the Graph tab glow together.
const GRAPH_KINDS = new Set(["graph_start", "node_start", "node_end", "route", "graph_end"]);
function animateGraphStage(ev){
  if (!document.querySelector(".graphchart")) return;
  const status = t => document.querySelectorAll(".arch-status").forEach(
    st => st.innerHTML = `<span class="live-dot"></span>${t}`);
  // Every graph event carries `workflow`, so the ids can be scoped to the chart
  // that is actually running instead of lighting every chart on the page.
  const w = ev.workflow || "";
  if (ev.type === "graph_start"){
    CODE_FOCUS = null;   // 新 run：清空读取焦点
    // Swap the Overview chart to this workflow before anything runs, so the
    // nodes about to light up are the ones on screen.
    graphLive(w);
    status(`${w} starts`);
  }
  else if (ev.type === "node_start"){
    CODE_FOCUS = ev.node;   // 当前读取节点 → 常显标签
    status(`${w} · ${_shortName(ev.node)}`);
    // Held, not pulsed: a node is lit for as long as it is WORKING. Pulsing on
    // node_end only ever showed you what had already finished, which is the
    // opposite of watching it happen — and with four nodes in one wave it is
    // the difference between seeing a fan-out and seeing four blinks.
    document.querySelectorAll(`[data-node="g-${w}-${ev.node}"]`)
      .forEach(el => el.classList.add("hot"));
  }
  else if (ev.type === "node_end"){
    document.querySelectorAll(`[data-node="g-${w}-${ev.node}"]`)
      .forEach(el => el.classList.remove("hot"));
    hot(`[data-node="g-${w}-${ev.node}"]`, "done", 900);
  }
  else if (ev.type === "route"){
    status(`route → ${ev.target}`);
    hot(`[data-edge="g-${w}-${ev.router}-${ev.target}"]`, "live", 1400);
    hot(`[data-node="g-${w}-${ev.target}"]`, "hot", 1400);
  }
  else if (ev.type === "graph_end"){
    CODE_FOCUS = null;
    graphLive(null);
  }
}

// Setting the live workflow re-renders, so the panel swaps the moment a run
// starts rather than at the next poll.
function graphLive(name){
  if (GRAPH_LIVE === name) return;
  if (name) GRAPH_SHOWN = name;   // sticky: outlives the run, so no flick-back
  GRAPH_LIVE = name;
  if (typeof render === "function") render();
}

// ---------------------------------------------------------------------------
// THE RUNNER — N nodes as a row of live cards.
//
// The topology chart above shows the SHAPE: "these four are independent". It
// cannot show that they actually ran together, because a picture has no time
// axis and a viewer cannot tell four boxes lit at once from four lit very fast
// in sequence. So the cards carry the time: they start together, tick while
// running, and finish out of order. Watching three settle while one spins is
// the proof the chart can only promise.
//
// Same shape as the Arena, deliberately — arena.py tags every event with `spec`
// and routes it to a card; the graph engine already tags every event with
// `node`. Swap the key, reuse .cmp-grid/.cmp-col, and it reads as a sibling
// because it is one.
let graphRun = {running: false, workflow: "", nodes: {}, order: [], waves: [],
                digest: "", draft: "", error: "", ticker: null};

function graphResetRun(workflow){
  graphRun = {running: true, workflow, nodes: {}, order: [], waves: [],
              digest: "", draft: "", error: "", ticker: graphRun.ticker};
}

function graphApplyEvent(ev){
  const R = graphRun;
  const k = ev.kind;
  if (k === "graph_start"){
    R.order = ev.nodes || [];
    R.order.forEach(n => R.nodes[n] = {status: "waiting"});
  } else if (k === "node_start"){
    // A wave is "the nodes that started before any of them finished". That is
    // exactly what the engine means by a wave, and it is what the row groups by.
    const open = R.waves[R.waves.length - 1];
    if (open && !open.closed) open.nodes.push(ev.node);
    else R.waves.push({nodes: [ev.node], closed: false});
    R.nodes[ev.node] = {status: "running", startedAt: performance.now()};
  } else if (k === "node_end"){
    const w = R.waves[R.waves.length - 1];
    if (w) w.closed = true;   // first finish closes the wave for new members
    R.nodes[ev.node] = {status: ev.error ? "error" : "done", ms: ev.ms,
                        keys: ev.keys || [], error: ev.error || ""};
  } else if (k === "route"){
    R.route = {target: ev.target, reason: ev.reason};
  } else if (k === "graph_end"){
    R.running = false; R.totalMs = ev.ms;
  } else if (k === "done"){
    R.running = false;
    R.digest = ev.digest || ""; R.draft = ev.draft_path || ""; R.error = ev.error || "";
  }
}

async function runGraph(workflow){
  if (graphRun.running) return;
  graphResetRun(workflow);
  // Without a ticker the elapsed numbers freeze and the cards look identical to
  // a sequential run — the one thing this view exists to disprove.
  clearInterval(graphRun.ticker);
  graphRun.ticker = setInterval(() => { if (graphRun.running) render(); }, 100);
  render();
  try {
    const res = await fetch("/api/graph/stream", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({workflow}),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;){
      const {value, done} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const p of parts){
        const line = p.trim();
        if (!line.startsWith("data:")) continue;
        try { graphApplyEvent(JSON.parse(line.slice(5))); } catch (e) { /* partial frame */ }
        render();
      }
    }
  } catch (e){
    graphRun.error = String(e);
  } finally {
    graphRun.running = false;
    clearInterval(graphRun.ticker);
    render();
  }
}

function graphCol(name){
  const n = graphRun.nodes[name] || {status: "waiting"};
  if (n.status === "waiting")
    return `<div class="cmp-col" style="opacity:.5"><div class="cmp-h"><b>${esc(name)}</b></div>
      <div class="meta">queued</div></div>`;
  if (n.status === "running"){
    const el = ((performance.now() - n.startedAt) / 1000).toFixed(1);
    return `<div class="cmp-col"><div class="cmp-h"><b>${esc(name)}</b></div>
      <div class="meta"><span class="live-dot"></span>${el}s</div></div>`;
  }
  if (n.status === "error")
    return `<div class="cmp-col err"><div class="cmp-h"><b>${esc(name)}</b></div>
      <div class="meta" style="color:var(--bad)">${esc(n.error)}</div></div>`;
  // The bar is scaled to the SLOWEST node in this node's wave, and every faster
  // node prints what it spent waiting at the barrier. That number is the honest
  // cost of wave execution — printing it teaches more than hiding it would.
  const wave = graphRun.waves.find(w => w.nodes.includes(name));
  const peers = (wave ? wave.nodes : [name]).map(x => (graphRun.nodes[x] || {}).ms || 0);
  const slowest = Math.max(...peers, 1);
  const pct = Math.round((n.ms || 0) / slowest * 100);
  const waited = slowest - (n.ms || 0);
  return `<div class="cmp-col"><div class="cmp-h"><b>${esc(name)}</b>
      <span class="chip">${n.ms}ms</span></div>
    <div class="wavebar"><i style="width:${pct}%"></i></div>
    <div class="meta">${waited > 20 && peers.length > 1
      ? `waited ${(waited/1000).toFixed(1)}s at the barrier`
      : (peers.length > 1 ? "set the pace for this wave" : "")}</div>
    <div class="meta">${(n.keys || []).map(k => `<span class="chip">${esc(k)}</span>`).join(" ")}</div>
  </div>`;
}

function graphRunPanel(){
  const R = graphRun;
  const btn = `<button class="btn" onclick="runGraph('gather')" ${R.running ? "disabled" : ""}>
    ${R.running ? "running…" : "Run gather"}</button>`;
  let h = `<h2>Run it — watch the wave <span class="meta" style="font-weight:400">
    the chart shows the shape; these cards show it happening</span></h2>
    <div class="card">${btn}
    <span class="meta" style="margin-left:10px">fetches GitHub, the web, your calendar and your
    memory — together. Proposes only: the digest lands in the outbox.</span>`;
  if (R.error) h += `<div class="meta" style="color:var(--bad);margin-top:10px">${esc(R.error)}</div>`;
  R.waves.forEach((w, i) => {
    const done = w.nodes.filter(n => (R.nodes[n] || {}).ms != null);
    const slowest = done.length ? Math.max(...done.map(n => R.nodes[n].ms)) : 0;
    const sum = done.reduce((a, n) => a + R.nodes[n].ms, 0);
    h += `<div class="meta" style="margin:14px 0 6px">wave ${i + 1} · ${w.nodes.length}
      node${w.nodes.length > 1 ? "s" : ""}${slowest ? ` · ${(slowest/1000).toFixed(1)}s`
      + (w.nodes.length > 1 ? ` (in sequence it would be ${(sum/1000).toFixed(1)}s)` : "") : ""}</div>
      <div class="cmp-grid">${w.nodes.map(graphCol).join("")}</div>`;
  });
  if (R.totalMs) h += `<div class="meta" style="margin-top:12px">finished in
    ${(R.totalMs/1000).toFixed(1)}s${R.draft ? ` · saved to <code>${esc(R.draft)}</code>` : ""}</div>`;
  if (R.digest) h += `<div class="card" style="margin-top:10px">${renderMarkdown(R.digest)}</div>`;
  return h + `</div>`;
}
