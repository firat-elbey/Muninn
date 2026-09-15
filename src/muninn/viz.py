"""Render the combined knowledge graph without external assets.

``render_html`` creates one offline HTML file with search, navigation, and
detail views. ``render_mermaid`` creates a bounded graph for Markdown or
chat. Node size represents retrieval strength, color represents provenance,
and edge style distinguishes authored links, co-use, and supersession.

Rendering is deterministic for a fixed bundle and sidecar state. The HTML
uses no external request.
"""

from __future__ import annotations

import json
import os

# provenance -> fill color (light/dark share these; they read on both)
_COLORS = {"curated": "#4c9be8", "extracted": "#57b26b", "inferred": "#c98bd9"}
_FALLBACK_COLOR = "#9aa3ad"

_MERMAID_TOP = 20          # default node cap for the chat-sized view
_MERMAID_LABEL_MAX = 36    # chars of a title shown per mermaid node


def _degree(graph: dict) -> dict[str, int]:
    deg: dict[str, int] = {}
    for e in graph.get("links", []):
        for end in (e.get("source"), e.get("target")):
            if end is not None:
                deg[end] = deg.get(end, 0) + 1
    return deg


def render_mermaid(graph: dict, top: int = _MERMAID_TOP) -> str:
    """A ``graph LR`` of the ``top`` most alive notes: strength first, then
    degree: with the edges that run among them. Superseded targets are
    marked. Small by design: it must render inside a chat window."""
    nodes = list(graph.get("nodes", []))
    deg = _degree(graph)
    nodes.sort(key=lambda n: (-float(n.get("strength", 0.0)),
                              -deg.get(n.get("id"), 0), str(n.get("id"))))
    keep = nodes[:max(1, top)]
    # a kept correction whose superseded partner fell below the cap would
    # render as an inexplicable island: pull the partner in so the
    # supersedes edge (the story) is visible
    kept_ids = {n.get("id") for n in keep}
    by_id = {n.get("id"): n for n in nodes}
    for e in graph.get("links", []):
        if e.get("kind") != "supersedes":
            continue
        s, t = e.get("source"), e.get("target")
        if (s in kept_ids) != (t in kept_ids):
            missing = by_id.get(t if s in kept_ids else s)
            if missing is not None:
                keep.append(missing)
                kept_ids.add(missing.get("id"))
    superseded = {e.get("target") for e in graph.get("links", [])
                  if e.get("kind") == "supersedes"}
    handle = {n["id"]: f"m{i}" for i, n in enumerate(keep)}

    def label(n: dict) -> str:
        text = str(n.get("label") or n.get("id") or "?")
        if len(text) > _MERMAID_LABEL_MAX:
            text = text[:_MERMAID_LABEL_MAX - 1].rstrip() + "…"
        text = text.replace('"', "#quot;")
        if n.get("provenance") == "inferred":
            text += " ✱"          # a model wrote this (color alone is easy to miss)
        if n["id"] in superseded:
            text += " ⊘"
        return text

    lines = ["graph LR",
             "  %% color: curated=blue · extracted=green · inferred=purple(✱)"
             " · ⊘=superseded · dotted=learned-from-use"]
    for n in keep:
        lines.append(f'  {handle[n["id"]]}["{label(n)}"]')
    seen: set[tuple[str, str, str]] = set()
    for e in graph.get("links", []):
        s, t = e.get("source"), e.get("target")
        if s not in handle or t not in handle:
            continue
        kind = e.get("kind", "link")
        key = (handle[s], handle[t], kind)
        if key in seen:
            continue
        seen.add(key)
        if kind == "supersedes":
            lines.append(f"  {handle[s]} -->|supersedes| {handle[t]}")
        elif kind == "used_with":
            lines.append(f"  {handle[s]} -.- {handle[t]}")
        else:
            lines.append(f"  {handle[s]} --- {handle[t]}")
    for n in keep:  # provenance color per node
        color = _COLORS.get(str(n.get("provenance")), _FALLBACK_COLOR)
        lines.append(f"  style {handle[n['id']]} fill:{color},color:#fff")
    return "\n".join(lines)


def render_html(graph: dict, title: str = "muninn: knowledge graph") -> str:
    """The full graph as ONE offline HTML document (no external requests :
    scripts, styles, and data are all inline). Note titles are user/imported
    content: every ``<`` in the embedded JSON is escaped to ``\\u003c`` (a
    valid JSON escape), so a hostile title can never close the script block,
    open a new tag, or start a ``<!--`` comment: and every DOM write in the
    page goes through textContent, never innerHTML with data."""
    payload = json.dumps(graph, ensure_ascii=False).replace("<", "\\u003c")
    safe_title = (title.replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;"))
    colors = json.dumps(_COLORS)
    return _HTML_TEMPLATE % {
        "title": safe_title,
        "graph": payload,
        "colors": colors,
        "fallback": _FALLBACK_COLOR,
    }


def write_html(graph: dict, out_path: str,
               title: str = "muninn: knowledge graph") -> tuple[str, int, int]:
    """Render and write; returns ``(path, node_count, edge_count)``."""
    html = render_html(graph, title=title)
    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path, len(graph.get("nodes", [])), len(graph.get("links", []))


# The whole page. %-formatting (not str.format) so the JS braces stay plain.
_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
  :root { --bg:#ffffff; --fg:#1c2430; --muted:#5b6572; --panel:#f4f6f8;
          --line:#d6dbe1; --accent:#4c9be8; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#12161c; --fg:#e8ecf1; --muted:#9aa3ad; --panel:#1a212b;
            --line:#2c3440; }
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--fg);
         font: 14px/1.45 system-ui, sans-serif; overflow: hidden; }
  #bar { position: fixed; top: 0; left: 0; right: 0; display: flex; gap: 10px;
         align-items: center; padding: 8px 12px; background: var(--panel);
         border-bottom: 1px solid var(--line); z-index: 2; flex-wrap: wrap; }
  #bar h1 { font-size: 14px; font-weight: 600; margin-right: 6px; }
  #q { padding: 4px 8px; border: 1px solid var(--line); border-radius: 6px;
       background: var(--bg); color: var(--fg); width: 220px; }
  #bar label { color: var(--muted); font-size: 12px; user-select: none; }
  .sw { display: inline-block; width: 10px; height: 10px; border-radius: 50%%;
        margin: 0 4px 0 10px; vertical-align: -1px; }
  .lg { color: var(--muted); font-size: 12px; }
  #stage { position: fixed; inset: 0; }
  #detail { position: fixed; right: 12px; top: 52px; width: 300px;
            max-height: 70vh; overflow: auto; background: var(--panel);
            border: 1px solid var(--line); border-radius: 8px; padding: 12px;
            display: none; z-index: 2; }
  #detail h2 { font-size: 14px; margin-bottom: 4px; }
  #detail .meta { color: var(--muted); font-size: 12px; margin-bottom: 8px;
                  word-break: break-all; }
  #detail ul { padding-left: 18px; font-size: 12px; }
  #detail li { margin: 2px 0; color: var(--muted); }
  #hint { position: fixed; bottom: 10px; left: 12px; color: var(--muted);
          font-size: 12px; z-index: 2; }
</style>
</head>
<body>
<div id="bar">
  <h1>%(title)s</h1>
  <input id="q" type="search" placeholder="search notes…" autocomplete="off">
  <label><input type="checkbox" id="k-link" checked> links</label>
  <label><input type="checkbox" id="k-used" checked> used-together</label>
  <label><input type="checkbox" id="k-sup" checked> supersedes</label>
  <span class="lg"><span class="sw" style="background:#4c9be8"></span>curated
    <span class="sw" style="background:#57b26b"></span>extracted
    <span class="sw" style="background:#c98bd9"></span>inferred
    &nbsp;·&nbsp; size = recall strength</span>
</div>
<canvas id="stage"></canvas>
<div id="detail"></div>
<div id="hint">drag nodes · drag background to pan · wheel to zoom · click a node for detail</div>
<script>
"use strict";
const GRAPH = %(graph)s;
const COLORS = %(colors)s, FALLBACK = "%(fallback)s";

const canvas = document.getElementById("stage"), ctx = canvas.getContext("2d");
let W, H, DPR = window.devicePixelRatio || 1;
function resize() {
  W = window.innerWidth; H = window.innerHeight;
  canvas.width = W * DPR; canvas.height = H * DPR;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
}
resize(); window.addEventListener("resize", () => { resize(); draw(); });

// deterministic start positions (seeded PRNG), physics settles the rest
function mulberry32(a) { return function() {
  a |= 0; a = a + 0x6D2B79F5 | 0;
  let t = Math.imul(a ^ a >>> 15, 1 | a);
  t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
  return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
const rand = mulberry32(42);

const nodes = (GRAPH.nodes || []).map((n, i) => ({
  ...n, i,
  r: 4 + Math.min(10, (n.strength || 0) * 7),
  x: (rand() - 0.5) * 900, y: (rand() - 0.5) * 700, vx: 0, vy: 0,
}));
const byId = new Map(nodes.map(n => [n.id, n]));
const superseded = new Set((GRAPH.links || [])
  .filter(e => e.kind === "supersedes").map(e => e.target));
const links = (GRAPH.links || [])
  .filter(e => byId.has(e.source) && byId.has(e.target))
  .map(e => ({ ...e, s: byId.get(e.source), t: byId.get(e.target) }));
const deg = new Map();
links.forEach(e => { deg.set(e.s.id, (deg.get(e.s.id) || 0) + 1);
                     deg.set(e.t.id, (deg.get(e.t.id) || 0) + 1); });

// simple force simulation: springs + repulsion (grid-bucketed) + centering
let alpha = 1.0;
function step() {
  const cell = 120, grid = new Map();
  nodes.forEach(n => {
    const k = (n.x / cell | 0) + ":" + (n.y / cell | 0);
    (grid.get(k) || grid.set(k, []).get(k)).push(n);
  });
  nodes.forEach(n => {
    const cx = n.x / cell | 0, cy = n.y / cell | 0;
    for (let gx = cx - 1; gx <= cx + 1; gx++)
      for (let gy = cy - 1; gy <= cy + 1; gy++) {
        const bucket = grid.get(gx + ":" + gy); if (!bucket) continue;
        for (const m of bucket) {
          if (m === n) continue;
          let dx = n.x - m.x, dy = n.y - m.y;
          let d2 = dx * dx + dy * dy || 0.01;
          if (d2 > cell * cell) continue;
          const f = 320 / d2;
          n.vx += dx * f * alpha; n.vy += dy * f * alpha;
        }
      }
  });
  links.forEach(e => {
    const want = 70 + 20 / (e.weight || 1);
    let dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const f = (d - want) / d * 0.02 * (e.weight || 1);
    e.s.vx += dx * f * alpha; e.s.vy += dy * f * alpha;
    e.t.vx -= dx * f * alpha; e.t.vy -= dy * f * alpha;
  });
  nodes.forEach(n => {
    n.vx -= n.x * 0.0016 * alpha; n.vy -= n.y * 0.0016 * alpha;
    if (n !== dragging) { n.x += n.vx; n.y += n.vy; }
    n.vx *= 0.85; n.vy *= 0.85;
  });
  alpha = Math.max(0.02, alpha * 0.995);
}

// view transform
let scale = 1, tx = 0, ty = 0;
function toScreen(n) { return [W / 2 + (n.x + tx) * scale,
                               H / 2 + (n.y + ty) * scale]; }
function toWorld(px, py) { return [(px - W / 2) / scale - tx,
                                   (py - H / 2) / scale - ty]; }

let query = "", selected = null;
const kinds = { link: true, typed: true, used_with: true, supersedes: true };
const css = v => getComputedStyle(document.documentElement)
                   .getPropertyValue(v).trim();

function draw() {
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const fg = css("--fg"), muted = css("--muted");
  ctx.lineWidth = 1;
  links.forEach(e => {
    // unknown/missing kinds stay VISIBLE (only an explicit false hides) :
    // a graph from another module may not stamp `kind` at all
    const kind = e.kind === "typed" ? "link" : e.kind;
    if (kinds[e.kind] === false || kinds[kind] === false) return;
    const [x1, y1] = toScreen(e.s), [x2, y2] = toScreen(e.t);
    ctx.beginPath();
    ctx.setLineDash(e.kind === "used_with" ? [3, 4] : []);
    ctx.strokeStyle = e.kind === "supersedes" ? "#d2604f" : muted;
    ctx.globalAlpha = e.kind === "supersedes" ? 0.9
                    : Math.min(0.75, 0.25 + (e.weight || 0.5) * 0.4);
    ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  });
  const q = query.toLowerCase();
  nodes.forEach(n => {
    const [x, y] = toScreen(n);
    if (x < -40 || y < -40 || x > W + 40 || y > H + 40) return;
    const hit = q && ((n.label || "").toLowerCase().includes(q) ||
                      (n.id || "").toLowerCase().includes(q));
    const dim = q && !hit;
    ctx.globalAlpha = dim ? 0.18 : 1;
    ctx.beginPath();
    ctx.fillStyle = superseded.has(n.id) ? FALLBACK
                  : (COLORS[n.provenance] || FALLBACK);
    ctx.arc(x, y, n.r * scale, 0, 7); ctx.fill();
    if (superseded.has(n.id)) {         // the corrected: ringed, faded
      ctx.beginPath(); ctx.strokeStyle = "#d2604f";
      ctx.arc(x, y, (n.r + 2.5) * scale, 0, 7); ctx.stroke();
    }
    if (n === selected || hit) {
      ctx.beginPath(); ctx.strokeStyle = fg; ctx.lineWidth = 1.5;
      ctx.arc(x, y, (n.r + 4) * scale, 0, 7); ctx.stroke(); ctx.lineWidth = 1;
    }
    if (scale > 0.55 && (n.r >= 7 || hit || n === selected ||
                         (deg.get(n.id) || 0) >= 6)) {
      ctx.fillStyle = dim ? muted : fg;
      ctx.font = "11px system-ui, sans-serif";
      ctx.fillText(n.label || n.id, x + n.r * scale + 4, y + 3);
    }
    ctx.globalAlpha = 1;
  });
}

function tick() { step(); draw(); requestAnimationFrame(tick); }
requestAnimationFrame(tick);

// interactions ---------------------------------------------------------
let dragging = null, panning = false, px = 0, py = 0, moved = 0;
function nodeAt(mx, my) {
  const [wx, wy] = toWorld(mx, my);
  let best = null, bd = 1e9;
  nodes.forEach(n => {
    const dx = n.x - wx, dy = n.y - wy, d = dx * dx + dy * dy;
    // node's WORLD radius is n.r (drawn at n.r*scale); only the 6px slop
    // converts from screen space: (n.r+6)/scale shrank hits when zoomed in
    const rr = n.r + 6 / scale;
    if (d < rr * rr && d < bd) { best = n; bd = d; }
  });
  return best;
}
canvas.addEventListener("mousedown", e => {
  const n = nodeAt(e.offsetX, e.offsetY);
  moved = 0;
  if (n) { dragging = n; alpha = Math.max(alpha, 0.3); }
  else { panning = true; }
  px = e.offsetX; py = e.offsetY;
});
window.addEventListener("mousemove", e => {
  const dx = e.clientX - px, dy = e.clientY - py;
  if (dragging) {
    dragging.x += dx / scale; dragging.y += dy / scale;
    px = e.clientX; py = e.clientY; moved += Math.abs(dx) + Math.abs(dy);
  } else if (panning) {
    tx += dx / scale; ty += dy / scale;
    px = e.clientX; py = e.clientY; moved += Math.abs(dx) + Math.abs(dy);
  }
});
window.addEventListener("mouseup", e => {
  if (dragging && moved < 4) select(dragging);
  else if (panning && moved < 4) select(null);
  dragging = null; panning = false;
});
canvas.addEventListener("wheel", e => {
  e.preventDefault();
  const f = e.deltaY < 0 ? 1.12 : 0.89;
  scale = Math.min(6, Math.max(0.12, scale * f));
}, { passive: false });

const detail = document.getElementById("detail");
function select(n) {
  selected = n;
  detail.innerHTML = "";
  if (!n) { detail.style.display = "none"; return; }
  const h = document.createElement("h2"); h.textContent = n.label || n.id;
  const meta = document.createElement("div"); meta.className = "meta";
  meta.textContent = n.id + " · " + (n.provenance || "curated")
    + " · strength " + (n.strength ?? 0)
    + (superseded.has(n.id) ? " · SUPERSEDED" : "");
  detail.appendChild(h); detail.appendChild(meta);
  const ul = document.createElement("ul");
  links.forEach(e => {
    if (e.s !== n && e.t !== n) return;
    const other = e.s === n ? e.t : e.s;
    const li = document.createElement("li");
    const rel = e.relation || e.kind;
    const suffix = e.kind === "used_with" ? " (learned from use)" : "";
    // direction-aware: an INCOMING edge must not read as if it were ours
    li.textContent = (e.s === n
      ? rel + " → " + (other.label || other.id)
      : "← " + rel + ": " + (other.label || other.id)) + suffix;
    ul.appendChild(li);
  });
  detail.appendChild(ul);
  detail.style.display = "block";
}

document.getElementById("q").addEventListener("input", e => {
  query = e.target.value; alpha = Math.max(alpha, 0.05);
});
for (const [id, key] of [["k-link", "link"], ["k-used", "used_with"],
                          ["k-sup", "supersedes"]]) {
  document.getElementById(id).addEventListener("change", e => {
    kinds[key] = e.target.checked;
    if (key === "link") kinds.typed = e.target.checked;
  });
}
</script>
</body>
</html>
"""
