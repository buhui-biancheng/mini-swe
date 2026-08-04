// fsm.js — FSM 状态流转图：实时展示 agent 在状态机里的移动。
// 状态与转移边来自 swe_agent/fsm/agent_fsm.py 的 TRANSITIONS（别手画漂移）。
// 事件源：trace 里 {"type":"state","state":"locate","attempt":N}，走同一个 450ms 轮询。
// 复用 .arch 的 node/bx/nt/ns/flow 样式 + hot() 机制，与代码依赖图同款观感。

// 8 个状态盒的位置（固定结构手摆；转移边是数据，不手画）
const FSM_POS = {
  init:     {x: 0,    y: 50},
  locate:   {x: 215,  y: 50},
  patch:    {x: 430,  y: 50},
  check:    {x: 645,  y: 50},
  test:     {x: 860,  y: 50},
  success:  {x: 1095, y: 50},
  rollback: {x: 645,  y: 190},
  fail:     {x: 1095, y: 190},
};
const FSM_W = 160, FSM_H = 54;
const FSM_LABELS = {
  init: "初始化", locate: "定位", patch: "补丁", check: "检查",
  test: "测试", rollback: "回滚", success: "成功", fail: "失败",
};

// 转移边（来自 agent_fsm.py TRANSITIONS，按语义归类着色）
const FSM_EDGES = [
  // 主干
  {src: "init",    dst: "locate",  cls: "fsm-norm"},
  {src: "locate",  dst: "patch",   cls: "fsm-norm"},
  {src: "patch",   dst: "check",   cls: "fsm-norm"},
  {src: "check",   dst: "test",    cls: "fsm-norm"},
  {src: "test",    dst: "success", cls: "fsm-norm"},
  // 重试回退（琥珀色）
  {src: "check",   dst: "patch",   cls: "fsm-retry"},     // check_fail 语法错可重试
  {src: "patch",   dst: "locate",  cls: "fsm-retry"},     // patch_syntax_error
  {src: "test",    dst: "locate",  cls: "fsm-retry"},     // test_fail 低影响面继续修
  {src: "rollback", dst: "locate", cls: "fsm-retry"},     // rollback_retry / degrade
  // 熔断回卷（红色）
  {src: "check",   dst: "rollback", cls: "fsm-rollback"}, // check_exhausted 连续语法失败
  {src: "test",    dst: "rollback", cls: "fsm-rollback"}, // 代价熔断
  // 失败出口
  {src: "locate",  dst: "fail", cls: "fsm-fail"},
  {src: "patch",   dst: "fail", cls: "fsm-fail"},
  {src: "test",    dst: "fail", cls: "fsm-fail"},
  {src: "rollback", dst: "fail", cls: "fsm-fail"},
];

let FSM_CURRENT = null;   // 当前状态（跨渲染保持，供 fsmChart 重新点亮）
let FSM_ATTEMPT = 0;

function fsmChart(){
  const W = 1255, H = 340;
  const box = n => {
    const p = FSM_POS[n], hotCls = FSM_CURRENT === n ? " hot" : "";
    return `<g class="node${hotCls}" data-node="g-fsm-${n}">
      <rect class="bx" x="${p.x}" y="${p.y}" width="${FSM_W}" height="${FSM_H}" rx="9"/>
      <text class="nt" x="${p.x + 12}" y="${p.y + 22}">${n}</text>
      <text class="ns" x="${p.x + 12}" y="${p.y + 40}">${FSM_LABELS[n] || ""}${FSM_CURRENT === n && FSM_ATTEMPT ? ` · 第 ${FSM_ATTEMPT} 次` : ""}</text>
    </g>`;
  };
  const edge = e => {
    const a = FSM_POS[e.src], b = FSM_POS[e.dst];
    const x1 = a.x + FSM_W, y1 = a.y + FSM_H / 2;
    const x2 = b.x, y2 = b.y + FSM_H / 2;
    const back = x2 < x1;   // 反向边（回退/熔断）下弯，避免与主干重叠
    const d = back
      ? `M${x1} ${y1} C${x1 + 70} ${y1 + 70} ${x2 - 70} ${y2 + 70} ${x2} ${y2}`
      : `M${x1} ${y1} C${(x1 + x2) / 2} ${y1} ${(x1 + x2) / 2} ${y2} ${x2} ${y2}`;
    return `<path class="flow ${e.cls}" data-edge="g-fsm-${e.src}-${e.dst}" d="${d}"/>`;
  };
  const zoom = (typeof _zoomState !== "undefined" ? _zoomState["fsm"] : null) || {x: 0, y: 0, w: W, h: H};
  return `<div style="overflow-x:auto"><svg viewBox="${zoom.x} ${zoom.y} ${zoom.w} ${zoom.h}" class="arch fsmchart" data-zoomkey="fsm" style="max-width:${W}px" role="img">
    <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"
      orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="head"/></marker></defs>
    ${FSM_EDGES.map(edge).join("")}
    ${Object.keys(FSM_POS).map(box).join("")}
  </svg></div>`;
}

// Overview / Graph tab 上的状态流转面板
function fsmPanel(){
  const label = FSM_LABELS[FSM_CURRENT] || "";
  const when = FSM_CURRENT
    ? `<span class="live-dot"></span>当前状态 <b>${FSM_CURRENT}</b>${label ? `（${label}）` : ""}${FSM_ATTEMPT ? ` · 第 ${FSM_ATTEMPT} 次尝试` : ""}`
    : "运行修复后，状态盒随 <code>[STATE]</code> 事件实时点亮";
  return `<div class="card">${fsmChart()}
    <div class="meta" style="margin-top:8px">${when}
      &nbsp;·&nbsp; <span style="color:var(--ink3)">■</span>主干
      <span style="color:#c9a227">■</span>重试回退
      <span style="color:var(--bad)">■</span>熔断/失败</div></div>`;
}

// 新任务开始 → 重置状态图
function fsmReset(){ FSM_CURRENT = null; FSM_ATTEMPT = 0; }

// trace 的 state 事件 → 熄灭前一状态、点亮当前、动画刚走过的转移边
function animateFSM(ev){
  const prev = FSM_CURRENT, curr = ev.state || "";
  if (!curr) return;
  FSM_CURRENT = curr;
  FSM_ATTEMPT = ev.attempt || 0;
  if (prev && prev !== curr){
    document.querySelectorAll(`[data-node="g-fsm-${prev}"]`).forEach(el => el.classList.remove("hot"));
    document.querySelectorAll(`[data-edge="g-fsm-${prev}-${curr}"]`).forEach(el => {
      el.classList.add("live");
      setTimeout(() => el.classList.remove("live"), 1500);
    });
  }
  document.querySelectorAll(`[data-node="g-fsm-${curr}"]`).forEach(el => el.classList.add("hot"));
  document.querySelectorAll(".arch-status").forEach(st =>
    st.innerHTML = `<span class="live-dot"></span>状态 → ${curr}`);
}
