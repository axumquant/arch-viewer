"""
Standalone interactive architecture diagram generator.

Produces a single self-contained HTML file (no external CSS/JS) styled to match
the Sales Coach reference diagram: pannable/zoomable world, color-tier node
cards with CSS glow, animated cubic-bezier SVG flow edges, layer column labels,
slide-in detail panel, legend, and topbar controls.

Public API:
    generate_interactive_html(arch, project_name) -> str
"""

from __future__ import annotations

import json
from html import escape
from typing import Iterable

from .models import Architecture, Component, ComponentType, DataFlow


# ─── Layout ──────────────────────────────────────────────────────────────────

# Column order — one column per component type. Types not in this list fall
# through to the trailing "other" column.
COLUMN_ORDER: list[ComponentType] = [
    ComponentType.FRONTEND,
    ComponentType.EXTENSION,
    ComponentType.API_GATEWAY,
    ComponentType.BACKEND,
    ComponentType.MCP_SERVER,
    ComponentType.WORKER,
    ComponentType.QUEUE,
    ComponentType.AUTH,
    ComponentType.CACHE,
    ComponentType.STORAGE,
    ComponentType.DATABASE,
    ComponentType.DOCKER,
    ComponentType.CI_CD,
    ComponentType.CONFIG,
    ComponentType.OTHER,
]

COLUMN_LABELS: dict[ComponentType, str] = {
    ComponentType.FRONTEND: "Frontend",
    ComponentType.EXTENSION: "Extension",
    ComponentType.API_GATEWAY: "API Gateway",
    ComponentType.BACKEND: "Backend",
    ComponentType.MCP_SERVER: "MCP Servers",
    ComponentType.WORKER: "Workers",
    ComponentType.QUEUE: "Queues",
    ComponentType.AUTH: "Auth",
    ComponentType.CACHE: "Cache",
    ComponentType.STORAGE: "Storage",
    ComponentType.DATABASE: "Databases",
    ComponentType.DOCKER: "Docker",
    ComponentType.CI_CD: "CI / CD",
    ComponentType.CONFIG: "Config",
    ComponentType.OTHER: "Other",
}

# Type -> tier class (drives CSS border/glow color)
TIER_BY_TYPE: dict[ComponentType, str] = {
    ComponentType.FRONTEND: "frontend",
    ComponentType.EXTENSION: "extension",
    ComponentType.API_GATEWAY: "gateway",
    ComponentType.BACKEND: "backend",
    ComponentType.MCP_SERVER: "mcp",
    ComponentType.WORKER: "worker",
    ComponentType.QUEUE: "queue",
    ComponentType.AUTH: "auth",
    ComponentType.CACHE: "cache",
    ComponentType.STORAGE: "storage",
    ComponentType.DATABASE: "database",
    ComponentType.DOCKER: "docker",
    ComponentType.CI_CD: "cicd",
    ComponentType.CONFIG: "config",
    ComponentType.OTHER: "other",
}

# Layout constants (CSS px in the "world" coordinate space)
COL_WIDTH = 230
COL_X_START = 60
NODE_X_OFFSET = 30          # node x within column
NODE_Y_START = 120
NODE_Y_STEP = 100
LAYER_LABEL_Y = 60


def _safe_id(name: str) -> str:
    """Make a string safe to use as a JS/DOM identifier."""
    out = []
    for ch in name:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    return "n_" + "".join(out)


def _protocol_class(protocol: str) -> str:
    """Map a DataFlow.protocol string to a CSS edge class."""
    p = (protocol or "").lower()
    if any(k in p for k in ("ws", "websocket", "socket")):
        return "ws"
    if "grpc" in p:
        return "grpc"
    if any(k in p for k in ("http", "rest", "api")):
        return "http"
    if any(k in p for k in ("queue", "mq", "kafka", "rabbit", "amqp", "pubsub", "sqs", "sns")):
        return "queue"
    if any(k in p for k in ("db", "sql", "query")):
        return "db"
    return "default"


def _layout(arch: Architecture) -> tuple[dict, list[dict], list[dict]]:
    """
    Position components into columns. Returns (nodes_by_name, nodes_list, layer_labels).
    """
    # Group components by type, preserving original order within each group
    by_type: dict[ComponentType, list[Component]] = {t: [] for t in COLUMN_ORDER}
    for c in arch.components:
        ct = c.type if c.type in by_type else ComponentType.OTHER
        by_type[ct].append(c)

    nodes_list: list[dict] = []
    nodes_by_name: dict[str, dict] = {}
    layer_labels: list[dict] = []

    col_index = 0
    for ctype in COLUMN_ORDER:
        comps = by_type.get(ctype, [])
        if not comps:
            continue
        x = COL_X_START + col_index * COL_WIDTH
        layer_labels.append({
            "text": COLUMN_LABELS[ctype],
            "x": x,
            "y": LAYER_LABEL_Y,
        })
        for i, comp in enumerate(comps):
            y = NODE_Y_START + i * NODE_Y_STEP
            node = {
                "id": _safe_id(comp.name),
                "name": comp.name,
                "label": comp.name,
                "sub": COLUMN_LABELS[ctype],
                "tier": TIER_BY_TYPE.get(ctype, "other"),
                "type": ctype.value,
                "x": x + NODE_X_OFFSET,
                "y": y,
                "path": comp.path or "",
                "description": comp.description or "",
                "tech_stack": list(comp.tech_stack or []),
                "files_count": len(comp.files or []),
                "routes_count": len(comp.api_routes or []),
                "api_routes": [
                    {
                        "method": r.method,
                        "path": r.path,
                        "file": r.file,
                    }
                    for r in (comp.api_routes or [])[:25]
                ],
            }
            nodes_list.append(node)
            nodes_by_name[comp.name] = node
        col_index += 1

    return nodes_by_name, nodes_list, layer_labels


def _build_flow_edges(
    flows: Iterable[DataFlow], nodes_by_name: dict[str, dict]
) -> list[dict]:
    edges: list[dict] = []
    for f in flows:
        src = nodes_by_name.get(f.source)
        tgt = nodes_by_name.get(f.target)
        if not src or not tgt:
            continue
        edges.append({
            "from": src["id"],
            "to": tgt["id"],
            "cls": _protocol_class(f.protocol),
            "protocol": f.protocol or "",
            "description": f.description or "",
            "bidi": (f.direction.value == "bidirectional") if hasattr(f.direction, "value") else False,
        })
    return edges


# ─── HTML template ───────────────────────────────────────────────────────────

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<style>
  *,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
  :root{
    --bg:#0a0e17;--surface:#111827;--border:#1e293b;
    --text:#e2e8f0;--muted:#64748b;
    --accent:#6366f1;--accent2:#8b5cf6;
    --glow-accent:0 0 14px rgba(99,102,241,.5);
  }
  html,body{height:100%;overflow:hidden;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text)}

  /* canvas */
  #canvas-wrap{position:absolute;inset:0;overflow:hidden;cursor:grab}
  #canvas-wrap.grabbing{cursor:grabbing}
  #world{position:absolute;transform-origin:0 0;will-change:transform}

  /* topbar */
  #topbar{position:fixed;top:0;left:0;right:0;height:52px;display:flex;align-items:center;justify-content:space-between;padding:0 24px;background:linear-gradient(180deg,rgba(10,14,23,.95),rgba(10,14,23,.7));backdrop-filter:blur(10px);z-index:100;border-bottom:1px solid var(--border)}
  #topbar h1{font-size:16px;font-weight:600;letter-spacing:.3px;background:linear-gradient(135deg,#818cf8,#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  #topbar .controls{display:flex;gap:8px}
  #topbar button{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px;transition:border-color .2s}
  #topbar button:hover{border-color:var(--accent)}

  /* legend */
  #legend{position:fixed;bottom:16px;left:16px;background:rgba(17,24,39,.92);border:1px solid var(--border);border-radius:10px;padding:14px 18px;z-index:100;backdrop-filter:blur(8px);font-size:12px;max-height:70vh;overflow-y:auto}
  #legend h3{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:8px}
  #legend h3:not(:first-child){margin-top:10px}
  .legend-row{display:flex;align-items:center;gap:8px;margin-bottom:5px}
  .legend-dot{width:10px;height:10px;border-radius:50%}
  .legend-line{width:24px;height:2px;border-radius:1px}

  /* detail panel */
  #detail{position:fixed;top:52px;right:0;width:360px;height:calc(100% - 52px);background:rgba(17,24,39,.96);border-left:1px solid var(--border);backdrop-filter:blur(12px);z-index:100;transform:translateX(100%);transition:transform .3s ease;padding:24px;overflow-y:auto}
  #detail.open{transform:translateX(0)}
  #detail .close-btn{position:absolute;top:12px;right:14px;background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer}
  #detail h2{font-size:16px;font-weight:600;margin-bottom:4px}
  #detail .tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;margin-bottom:12px;color:#fff}
  #detail p{font-size:13px;color:var(--muted);line-height:1.6;margin-bottom:12px}
  #detail h4{font-size:12px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin:16px 0 6px}
  #detail ul{list-style:none;padding:0}
  #detail li{font-size:13px;padding:4px 0;border-bottom:1px solid var(--border);color:var(--text)}
  #detail li:last-child{border:none}
  #detail code{font-family:'Cascadia Code','Fira Code',monospace;font-size:12px;color:#a5b4fc;background:rgba(99,102,241,.12);padding:1px 5px;border-radius:3px}
  #detail .chip{display:inline-block;background:rgba(99,102,241,.12);color:#a5b4fc;border:1px solid rgba(99,102,241,.3);padding:2px 8px;border-radius:10px;font-size:11px;margin:2px 4px 2px 0}

  /* node */
  .node{position:absolute;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 14px;cursor:pointer;transition:box-shadow .25s,border-color .25s,transform .18s;min-width:150px;text-align:center;user-select:none}
  .node:hover{transform:scale(1.05);z-index:10}
  .node .label{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}
  .node .sub{font-size:10px;color:var(--muted);margin-top:2px}
  .node.selected{border-color:var(--accent);box-shadow:var(--glow-accent)}

  /* tier colours — match Sales Coach palette where possible */
  .node.tier-frontend{border-color:#3b82f6;box-shadow:0 0 10px rgba(59,130,246,.25)}
  .node.tier-frontend:hover{box-shadow:0 0 18px rgba(59,130,246,.45)}
  .node.tier-extension{border-color:#22d3ee;box-shadow:0 0 10px rgba(34,211,238,.25)}
  .node.tier-extension:hover{box-shadow:0 0 18px rgba(34,211,238,.45)}
  .node.tier-gateway{border-color:#f97316;box-shadow:0 0 10px rgba(249,115,22,.25)}
  .node.tier-gateway:hover{box-shadow:0 0 18px rgba(249,115,22,.45)}
  .node.tier-backend{border-color:#22c55e;box-shadow:0 0 10px rgba(34,197,94,.25)}
  .node.tier-backend:hover{box-shadow:0 0 18px rgba(34,197,94,.45)}
  .node.tier-mcp{border-color:#818cf8;box-shadow:0 0 10px rgba(129,140,248,.25)}
  .node.tier-mcp:hover{box-shadow:0 0 18px rgba(129,140,248,.45)}
  .node.tier-worker{border-color:#facc15;box-shadow:0 0 10px rgba(250,204,21,.25)}
  .node.tier-worker:hover{box-shadow:0 0 18px rgba(250,204,21,.45)}
  .node.tier-queue{border-color:#fb7185;box-shadow:0 0 10px rgba(251,113,133,.25)}
  .node.tier-queue:hover{box-shadow:0 0 18px rgba(251,113,133,.45)}
  .node.tier-auth{border-color:#eab308;box-shadow:0 0 10px rgba(234,179,8,.25)}
  .node.tier-auth:hover{box-shadow:0 0 18px rgba(234,179,8,.45)}
  .node.tier-cache{border-color:#f472b6;box-shadow:0 0 10px rgba(244,114,182,.25)}
  .node.tier-cache:hover{box-shadow:0 0 18px rgba(244,114,182,.45)}
  .node.tier-storage{border-color:#a3e635;box-shadow:0 0 10px rgba(163,230,53,.25)}
  .node.tier-storage:hover{box-shadow:0 0 18px rgba(163,230,53,.45)}
  .node.tier-database{border-color:#a855f7;box-shadow:0 0 10px rgba(168,85,247,.3)}
  .node.tier-database:hover{box-shadow:0 0 18px rgba(168,85,247,.5)}
  .node.tier-docker{border-color:#38bdf8;box-shadow:0 0 10px rgba(56,189,248,.25)}
  .node.tier-docker:hover{box-shadow:0 0 18px rgba(56,189,248,.45)}
  .node.tier-cicd{border-color:#14b8a6;box-shadow:0 0 10px rgba(20,184,166,.25)}
  .node.tier-cicd:hover{box-shadow:0 0 18px rgba(20,184,166,.45)}
  .node.tier-config{border-color:#94a3b8;box-shadow:0 0 8px rgba(148,163,184,.25)}
  .node.tier-config:hover{box-shadow:0 0 18px rgba(148,163,184,.4)}
  .node.tier-other{border-color:#6b7280;box-shadow:0 0 8px rgba(107,114,128,.25)}

  /* layer labels */
  .layer-label{position:absolute;font-size:11px;text-transform:uppercase;letter-spacing:2px;color:var(--muted);font-weight:700;opacity:.55;pointer-events:none}

  /* SVG edges */
  svg.edges{position:absolute;top:0;left:0;pointer-events:none;overflow:visible}
  .edge{fill:none;stroke-width:1.4;opacity:.4}
  .edge.http{stroke:#3b82f6}
  .edge.ws{stroke:#f472b6}
  .edge.grpc{stroke:#a855f7}
  .edge.queue{stroke:#fb7185}
  .edge.db{stroke:#a3e635}
  .edge.default{stroke:#94a3b8}

  /* animated dash */
  @keyframes dash{to{stroke-dashoffset:-24}}
  .edge-anim{stroke-dasharray:8 16;animation:dash 1.2s linear infinite;opacity:.6}

  /* zoom hint */
  #zoom-hint{position:fixed;bottom:16px;right:16px;font-size:11px;color:var(--muted);z-index:100;background:rgba(17,24,39,.8);padding:6px 12px;border-radius:6px;border:1px solid var(--border)}

  /* empty state */
  #empty{position:fixed;inset:52px 0 0 0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:14px;pointer-events:none}
</style>
</head>
<body>

<div id="topbar">
  <h1>__TITLE__</h1>
  <div class="controls">
    <button onclick="resetView()">Reset View</button>
    <button onclick="toggleFlows()">Toggle Flow Animation</button>
  </div>
</div>

<div id="legend">
  <h3>Components</h3>
  __LEGEND_TIERS__
  <h3>Flow Protocols</h3>
  <div class="legend-row"><div class="legend-line" style="background:#3b82f6"></div>HTTP / REST</div>
  <div class="legend-row"><div class="legend-line" style="background:#f472b6"></div>WebSocket</div>
  <div class="legend-row"><div class="legend-line" style="background:#a855f7"></div>gRPC</div>
  <div class="legend-row"><div class="legend-line" style="background:#fb7185"></div>Queue / MQ</div>
  <div class="legend-row"><div class="legend-line" style="background:#a3e635"></div>DB</div>
  <div class="legend-row"><div class="legend-line" style="background:#94a3b8"></div>Other</div>
</div>

<div id="detail">
  <button class="close-btn" onclick="closeDetail()">&times;</button>
  <h2 id="d-title"></h2>
  <span class="tag" id="d-tag"></span>
  <p id="d-desc"></p>
  <h4>Path</h4>
  <p><code id="d-path"></code></p>
  <h4 id="d-tech-h" style="display:none">Tech Stack</h4>
  <div id="d-tech"></div>
  <h4 id="d-routes-h" style="display:none">API Routes</h4>
  <ul id="d-routes"></ul>
  <h4>Connections</h4>
  <ul id="d-conns"></ul>
</div>

<div id="canvas-wrap">
  <div id="world">
    <svg class="edges" id="edge-svg" width="2400" height="1200"></svg>
  </div>
</div>

<div id="zoom-hint">Scroll to zoom &middot; Drag to pan &middot; Click nodes for details</div>

<script>
const NODES = __NODES_JSON__;
const FLOWS = __FLOWS_JSON__;
const LAYER_LABELS = __LAYERS_JSON__;
const TIER_COLORS = {
  frontend:'#3b82f6', extension:'#22d3ee', gateway:'#f97316', backend:'#22c55e',
  mcp:'#818cf8', worker:'#facc15', queue:'#fb7185', auth:'#eab308',
  cache:'#f472b6', storage:'#a3e635', database:'#a855f7', docker:'#38bdf8',
  cicd:'#14b8a6', config:'#94a3b8', other:'#6b7280'
};
const TIER_NAMES = {
  frontend:'Frontend', extension:'Extension', gateway:'API Gateway', backend:'Backend',
  mcp:'MCP Server', worker:'Worker', queue:'Queue', auth:'Auth',
  cache:'Cache', storage:'Storage', database:'Database', docker:'Docker',
  cicd:'CI/CD', config:'Config', other:'Other'
};

const world  = document.getElementById('world');
const svgEl  = document.getElementById('edge-svg');
const wrap   = document.getElementById('canvas-wrap');
const nodeEls = {};
let selected = null;
let flowsOn  = true;

// Layer labels
LAYER_LABELS.forEach(l => {
  const d = document.createElement('div');
  d.className = 'layer-label';
  d.style.left = l.x + 'px';
  d.style.top  = l.y + 'px';
  d.textContent = l.text;
  world.appendChild(d);
});

// Nodes
NODES.forEach(n => {
  const el = document.createElement('div');
  el.className = 'node tier-' + n.tier;
  el.style.left = n.x + 'px';
  el.style.top  = n.y + 'px';
  el.innerHTML = '<div class="label"></div><div class="sub"></div>';
  el.querySelector('.label').textContent = n.label;
  el.querySelector('.sub').textContent = n.sub;
  el.addEventListener('click', e => { e.stopPropagation(); selectNode(n.id); });
  world.appendChild(el);
  nodeEls[n.id] = el;
});

if (NODES.length === 0) {
  const empty = document.createElement('div');
  empty.id = 'empty';
  empty.textContent = 'No components to display.';
  document.body.appendChild(empty);
}

function nodeCenter(id) {
  const el = nodeEls[id];
  if (!el) return {x:0,y:0};
  return {
    x: parseInt(el.style.left) + el.offsetWidth / 2,
    y: parseInt(el.style.top)  + el.offsetHeight / 2
  };
}

function buildEdges() {
  svgEl.innerHTML = '';
  FLOWS.forEach(f => {
    const a = nodeCenter(f.from);
    const b = nodeCenter(f.to);
    const dx = b.x - a.x;
    const cx1 = a.x + dx * 0.4;
    const cy1 = a.y;
    const cx2 = b.x - dx * 0.4;
    const cy2 = b.y;
    const path = document.createElementNS('http://www.w3.org/2000/svg','path');
    path.setAttribute('d', 'M'+a.x+','+a.y+' C'+cx1+','+cy1+' '+cx2+','+cy2+' '+b.x+','+b.y);
    path.setAttribute('class', 'edge ' + f.cls + (flowsOn ? ' edge-anim' : ''));
    svgEl.appendChild(path);
  });
}

requestAnimationFrame(() => {
  buildEdges();
  resetView();
});

function selectNode(id) {
  const n = NODES.find(x => x.id === id);
  if (!n) return;
  Object.values(nodeEls).forEach(el => el.classList.remove('selected'));
  nodeEls[id].classList.add('selected');
  selected = id;

  document.getElementById('d-title').textContent = n.label;
  const tag = document.getElementById('d-tag');
  tag.textContent = TIER_NAMES[n.tier] || n.tier;
  tag.style.background = TIER_COLORS[n.tier] || '#555';
  document.getElementById('d-desc').textContent = n.description || '(No description)';
  document.getElementById('d-path').textContent = n.path || '(no path)';

  // Tech stack
  const techH = document.getElementById('d-tech-h');
  const techDiv = document.getElementById('d-tech');
  techDiv.innerHTML = '';
  if (n.tech_stack && n.tech_stack.length) {
    techH.style.display = '';
    n.tech_stack.forEach(t => {
      const span = document.createElement('span');
      span.className = 'chip';
      span.textContent = t;
      techDiv.appendChild(span);
    });
  } else {
    techH.style.display = 'none';
  }

  // Routes
  const routesH = document.getElementById('d-routes-h');
  const routesUl = document.getElementById('d-routes');
  routesUl.innerHTML = '';
  if (n.api_routes && n.api_routes.length) {
    routesH.style.display = '';
    n.api_routes.forEach(r => {
      const li = document.createElement('li');
      li.innerHTML = '<code></code> <span class="rpath"></span>';
      li.querySelector('code').textContent = r.method;
      li.querySelector('.rpath').textContent = ' ' + r.path;
      routesUl.appendChild(li);
    });
  } else {
    routesH.style.display = 'none';
  }

  // Connections
  const ul = document.getElementById('d-conns');
  ul.innerHTML = '';
  const outgoing = FLOWS.filter(f => f.from === id).map(f => f.to);
  const incoming = FLOWS.filter(f => f.to === id).map(f => f.from);
  const all = [...new Set([...outgoing, ...incoming])];
  if (!all.length) {
    const li = document.createElement('li');
    li.style.color = 'var(--muted)';
    li.textContent = '(no flows)';
    ul.appendChild(li);
  }
  all.forEach(cid => {
    const cn = NODES.find(x => x.id === cid);
    if (!cn) return;
    const li = document.createElement('li');
    const arrow = outgoing.includes(cid) ? (incoming.includes(cid) ? '↔' : '→') : '←';
    li.textContent = arrow + ' ' + cn.label;
    li.style.cursor = 'pointer';
    li.addEventListener('click', () => selectNode(cid));
    ul.appendChild(li);
  });

  document.getElementById('detail').classList.add('open');
}

function closeDetail() {
  document.getElementById('detail').classList.remove('open');
  Object.values(nodeEls).forEach(el => el.classList.remove('selected'));
  selected = null;
}

wrap.addEventListener('click', e => {
  if (e.target === wrap || e.target === world) closeDetail();
});

// Pan & zoom
let scale = 1, tx = 0, ty = 0;
let dragging = false, dragX = 0, dragY = 0;

function applyTransform() {
  world.style.transform = 'translate('+tx+'px,'+ty+'px) scale('+scale+')';
}

wrap.addEventListener('mousedown', e => {
  if (e.button !== 0) return;
  dragging = true;
  dragX = e.clientX; dragY = e.clientY;
  wrap.classList.add('grabbing');
});
window.addEventListener('mousemove', e => {
  if (!dragging) return;
  tx += e.clientX - dragX;
  ty += e.clientY - dragY;
  dragX = e.clientX; dragY = e.clientY;
  applyTransform();
});
window.addEventListener('mouseup', () => {
  dragging = false;
  wrap.classList.remove('grabbing');
});

wrap.addEventListener('wheel', e => {
  e.preventDefault();
  const rect = wrap.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  const prevScale = scale;
  scale *= e.deltaY < 0 ? 1.08 : 0.926;
  scale = Math.max(0.25, Math.min(3, scale));
  tx = mx - (mx - tx) * (scale / prevScale);
  ty = my - (my - ty) * (scale / prevScale);
  applyTransform();
}, {passive:false});

function resetView() {
  if (!NODES.length) { tx = 0; ty = 52; scale = 1; applyTransform(); return; }
  let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
  NODES.forEach(n => {
    const el = nodeEls[n.id];
    const w = el.offsetWidth || 150;
    const h = el.offsetHeight || 50;
    if (n.x < minX) minX = n.x;
    if (n.y < minY) minY = n.y;
    if (n.x + w > maxX) maxX = n.x + w;
    if (n.y + h > maxY) maxY = n.y + h;
  });
  const pad = 80;
  const bw = maxX - minX + pad*2;
  const bh = maxY - minY + pad*2;
  const vw = wrap.clientWidth;
  const vh = wrap.clientHeight - 52;
  scale = Math.min(vw / bw, vh / bh, 1.2);
  tx = (vw - bw * scale) / 2 - (minX - pad) * scale;
  ty = 52 + (vh - bh * scale) / 2 - (minY - pad) * scale;
  applyTransform();
}

function toggleFlows() {
  flowsOn = !flowsOn;
  buildEdges();
}

window.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeDetail();
});
window.addEventListener('resize', () => { buildEdges(); });
</script>
</body>
</html>
"""


def _legend_tiers_html(tiers_present: list[str]) -> str:
    """Build legend rows for the tiers actually shown in the diagram."""
    color_map = {
        "frontend": "#3b82f6",
        "extension": "#22d3ee",
        "gateway": "#f97316",
        "backend": "#22c55e",
        "mcp": "#818cf8",
        "worker": "#facc15",
        "queue": "#fb7185",
        "auth": "#eab308",
        "cache": "#f472b6",
        "storage": "#a3e635",
        "database": "#a855f7",
        "docker": "#38bdf8",
        "cicd": "#14b8a6",
        "config": "#94a3b8",
        "other": "#6b7280",
    }
    label_map = {
        "frontend": "Frontend", "extension": "Extension", "gateway": "API Gateway",
        "backend": "Backend", "mcp": "MCP Server", "worker": "Worker",
        "queue": "Queue", "auth": "Auth", "cache": "Cache", "storage": "Storage",
        "database": "Database", "docker": "Docker", "cicd": "CI/CD",
        "config": "Config", "other": "Other",
    }
    rows = []
    for t in tiers_present:
        c = color_map.get(t, "#6b7280")
        n = label_map.get(t, t.title())
        rows.append(
            f'<div class="legend-row"><div class="legend-dot" style="background:{c}"></div>{escape(n)}</div>'
        )
    return "\n  ".join(rows) if rows else '<div class="legend-row" style="color:var(--muted)">(none)</div>'


def generate_interactive_html(arch: Architecture, project_name: str) -> str:
    """
    Render a complete self-contained interactive architecture diagram HTML.

    No external CSS/JS — everything is inlined.

    Args:
        arch: The Architecture model to render.
        project_name: Used in the page title and topbar.

    Returns:
        A complete HTML document as a string.
    """
    nodes_by_name, nodes_list, layer_labels = _layout(arch)
    edges = _build_flow_edges(arch.data_flows, nodes_by_name)

    # Determine which tiers are actually present (for the legend)
    tiers_present: list[str] = []
    seen: set[str] = set()
    for n in nodes_list:
        if n["tier"] not in seen:
            seen.add(n["tier"])
            tiers_present.append(n["tier"])

    title = f"{project_name} — System Architecture"

    html = (
        _HTML_TEMPLATE
        .replace("__TITLE__", escape(title))
        .replace("__NODES_JSON__", json.dumps(nodes_list))
        .replace("__FLOWS_JSON__", json.dumps(edges))
        .replace("__LAYERS_JSON__", json.dumps(layer_labels))
        .replace("__LEGEND_TIERS__", _legend_tiers_html(tiers_present))
    )
    return html
