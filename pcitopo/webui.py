"""The visual tool: one self-contained HTML page, built from the same model objects.

Like render.py and export.py, nothing in here decodes anything. It is a third
renderer over the same `Topology`, which is why adding it changed no decoder.

The page is a single file with the data, the stylesheet and the script all
inlined. No CDN, no build step, no server required: `--html topo.html` writes
it and any browser opens it, on Linux or Windows or a phone. That constraint is
the same pure-stdlib rule the rest of the repo follows, applied to the front end.

The drawing is a family tree: the root complex at the top, each device in a card
below its parent, joined by drawn connectors. The shape is the point -- it is
the same parent/child relation `topology.py` derives from Secondary Bus Numbers,
and seeing a card sit *under* a root port is seeing what that register means.

The root complex gets a card of its own even though it is not a PCI function.
Bus 0 is presented by it directly and nothing forwards to that bus, so on a real
machine every device on bus 0 is a sibling with no parent device. Drawing them
as orphaned stumps would be true but unreadable; giving them the one thing that
does sit above them is both honest and what lspci's own `-[0000:00]-` does.

The layout is CSS, not computed geometry. Each subtree is a `<ul>` of `<li>`
cards, and the connectors are borders on pseudo-elements, so the browser's own
flexbox centring does the positioning. There is no layout maths to get wrong and
nothing to recompute when a branch is collapsed.

Two ways the page gets its data, and the difference matters:

- **Standalone.** The topology is baked into the page as JSON at generation
  time. Dropping another `pcitopo --json` file re-renders it instantly, because
  that file is already decoded and the page only has to draw it.
- **Served** (`pcitopo serve`). The page can also POST a dropped file to the
  local Python process, which decodes it with the real decoders and sends back
  the same JSON shape. This is how an `lspci -vvv -xxxx` dump is accepted.

What the page deliberately does NOT do is decode configuration space in
JavaScript. A second decoder would be a second set of answers, and the moment
the two disagreed there would be no way to tell which was right. So the page
draws; Python decodes. When a standalone page is handed a file it cannot draw,
it says which command turns it into one it can, instead of guessing.
"""

import json
from datetime import datetime, timezone

from .ids import PciIds
from .model import Device
from .export import node_dict
from .render import render_access_note, render_scan_summary
from .sysfs import Scan
from .topology import Topology

PAGE_TITLE = "PCIe Topology"


def build_payload(
    topo: Topology,
    ids: PciIds,
    result: Scan,
    devices: list[Device],
    source: str = "",
) -> dict:
    """Everything the page needs, in one JSON-serialisable dict.

    The topology half is exactly what `export.to_json` produces, so the page and
    the `--json` flag cannot drift apart. The meta half is the text the terminal
    output prints above the tree: where the bytes came from and how complete
    they are. A viewer that showed the tree without the access note would let
    someone read an unprivileged scan as though it were the whole picture.
    """
    return {
        "meta": {
            "source": source or str(result.devices_dir),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "summary": render_scan_summary(result, devices, ids),
            "access_note": render_access_note(result),
            "warnings": list(topo.warnings),
            "device_count": len(devices),
            "bridge_count": sum(1 for d in devices if d.is_bridge),
            "degraded_count": sum(1 for d in devices if d.degraded),
            "cross_checked": topo.checked_against_sysfs,
            "config_sizes": {str(k): v for k, v in result.config_sizes.items()},
        },
        "domains": [
            {
                "domain": tree.domain,
                "root_bus": tree.root_bus,
                "devices": [node_dict(n, ids) for n in tree.roots],
                "orphans": [node_dict(n, ids) for n in tree.orphans],
            }
            for tree in topo.domains
        ],
    }


STYLE = """
:root {
  color-scheme: light dark;
  --bg: #f4f6f8; --panel: #ffffff; --ink: #14171a; --muted: #5b6570;
  --line: #ccd4dc; --accent: #2b6cb0; --accent-soft: #e8f0f8;
  --bridge: #7a5bb5; --bridge-soft: #f0ebf9;
  --bad: #b03030; --bad-soft: #fdeeee;
  --shadow: 0 1px 2px rgba(15,20,25,.07), 0 3px 8px rgba(15,20,25,.05);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #13161a; --panel: #1d2126; --ink: #e6e9ec; --muted: #94a0ac;
    --line: #39424b; --accent: #6aa9e0; --accent-soft: #1d2b38;
    --bridge: #ab8ee0; --bridge-soft: #262036;
    --bad: #e8807a; --bad-soft: #3a2323;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 3px 10px rgba(0,0,0,.3);
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
code, .mono, .addr { font-family: ui-monospace, "Cascadia Code", Consolas, "DejaVu Sans Mono", monospace; }

header { padding: 16px 22px 12px; border-bottom: 1px solid var(--line); background: var(--panel); }
h1 { margin: 0 0 2px; font-size: 17px; font-weight: 640; letter-spacing: -.01em; }
.sub { color: var(--muted); font-size: 12.5px; }
.stats { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 10px; }
.stat { background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 5px 11px; }
.stat b { font-size: 16px; font-weight: 640; display: block; line-height: 1.2; }
.stat span { font-size: 10.5px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }

.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 9px 22px;
  border-bottom: 1px solid var(--line); background: var(--panel); position: sticky; top: 0; z-index: 5; }
input[type=search] { flex: 1 1 200px; min-width: 150px; padding: 6px 10px; border-radius: 7px;
  border: 1px solid var(--line); background: var(--bg); color: var(--ink); font-size: 13px; }
button { padding: 6px 11px; border-radius: 7px; border: 1px solid var(--line);
  background: var(--bg); color: var(--ink); font-size: 12.5px; cursor: pointer; }
button:hover { border-color: var(--accent); color: var(--accent); }
button.on { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
.zoom { display: inline-flex; align-items: center; gap: 4px; }
.zoom span { font-size: 11.5px; color: var(--muted); min-width: 34px; text-align: center; }

main { display: grid; grid-template-columns: minmax(0,1fr) 340px; align-items: start; }
@media (max-width: 900px) { main { grid-template-columns: 1fr; }
  #detail { border-left: none; border-top: 1px solid var(--line); position: static; max-height: none; } }

/* --- the family tree --- */
#tree { padding: 26px 22px 70px; overflow: auto; }
/* `zoom`, not `transform: scale()`: a transform is painted after layout, so the
   scroll area would keep the unscaled width and the scrollbars would lie. */
#stage { zoom: 1; }
.domain { margin: 0 auto 40px; width: max-content; min-width: 100%; }
.domain > h2 { font-size: 11.5px; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted); margin: 0 0 14px; font-weight: 620; text-align: center; }

/* Each level is a row of <li>; the connectors are borders on pseudo-elements, so
   flexbox does the positioning and there is no layout arithmetic anywhere. */
.branch { display: flex; justify-content: center; list-style: none; margin: 0;
  padding: 22px 0 0; position: relative; }
.branch.top { padding-top: 0; }
/* the stem dropping from a parent card into its children's horizontal bar */
.branch::before { content: ""; position: absolute; top: 0; left: 50%;
  border-left: 1.5px solid var(--line); width: 0; height: 22px; }
.branch.top::before { display: none; }
/* The root complex has nothing above it, so it gets no stem and no bar. Without
   this it inherits the only-child rules below and grows a line into empty space. */
.branch.top > li { padding-top: 0; }
.branch.top > li::before, .branch.top > li::after { display: none; }

.branch > li { list-style: none; display: flex; flex-direction: column; align-items: center;
  position: relative; padding: 22px 7px 0; }
/* the two halves of the horizontal bar above each card */
.branch > li::before, .branch > li::after { content: ""; position: absolute; top: 0;
  border-top: 1.5px solid var(--line); width: 50%; height: 22px; }
.branch > li::before { right: 50%; }
.branch > li::after  { left: 50%; border-left: 1.5px solid var(--line); }
/* an only child needs a straight stem, not a bar with two stubs */
.branch > li:only-child { padding-top: 22px; }
.branch > li:only-child::before { display: none; }
.branch > li:only-child::after { left: 50%; width: 0; }
/* the outermost stubs would hang off the ends of the bar */
.branch > li:first-child::before { border: 0; }
.branch > li:last-child::after { border-top: 0; }

.card { width: 186px; background: var(--panel); border: 1px solid var(--line);
  border-top: 3px solid var(--muted); border-radius: 9px; padding: 9px 10px 8px;
  box-shadow: var(--shadow); cursor: pointer; text-align: center;
  transition: border-color .12s, transform .12s, box-shadow .12s; }
.card:hover { transform: translateY(-1px); border-color: var(--accent); }
.card.sel { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft), var(--shadow); }
.card.bridge { border-top-color: var(--bridge); }
.card.rc { border-top-color: var(--accent); background: var(--accent-soft); width: 210px; }
.card.degraded { border-top-color: var(--bad); background: var(--bad-soft); }
.card.dim { opacity: .32; }

.card .addr { display: block; font-size: 11.5px; color: var(--accent); font-weight: 640; }
.card .nm { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; font-size: 12px; line-height: 1.35; margin: 3px 0 5px; min-height: 32px; }
.card .role { font-size: 10px; text-transform: uppercase; letter-spacing: .04em;
  color: var(--muted); font-weight: 620; }
.card.bridge .role { color: var(--bridge); }
.card .lnk { display: block; font-size: 11px; color: var(--muted); margin-top: 4px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card.degraded .lnk { color: var(--bad); font-weight: 620; }
.card .bus { display: inline-block; font-size: 10px; color: var(--muted);
  border: 1px solid var(--line); border-radius: 20px; padding: 0 6px; margin-top: 4px; }

.twist { position: absolute; bottom: -11px; left: 50%; transform: translateX(-50%);
  width: 22px; height: 22px; border-radius: 50%; border: 1px solid var(--line);
  background: var(--panel); color: var(--muted); font-size: 11px; line-height: 20px;
  text-align: center; cursor: pointer; z-index: 2; user-select: none; }
.twist:hover { border-color: var(--accent); color: var(--accent); }
.holder { position: relative; }
li.collapsed > .branch { display: none; }

#detail { position: sticky; top: 47px; max-height: calc(100vh - 47px); overflow-y: auto;
  border-left: 1px solid var(--line); background: var(--panel); padding: 16px 18px 40px; }
#detail h3 { margin: 0 0 3px; font-size: 14px; }
#detail .addr { font-size: 13px; color: var(--accent); }
.sect { margin-top: 16px; }
.sect h4 { margin: 0 0 6px; font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); font-weight: 620; }
.kv { display: grid; grid-template-columns: 116px minmax(0,1fr); gap: 3px 10px; font-size: 12.5px; margin: 0; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; word-break: break-word; }
.empty { color: var(--muted); font-size: 13px; }

.note { margin: 0 22px 14px; padding: 10px 13px; border-radius: 9px; font-size: 12.5px;
  background: var(--panel); border: 1px solid var(--line); color: var(--muted); }
.note.warn { border-color: var(--bad); background: var(--bad-soft); color: var(--bad); }
.note pre { margin: 6px 0 0; white-space: pre-wrap; font-size: 12px; }

#drop { position: fixed; inset: 0; background: rgba(20,30,45,.72); color: #fff;
  display: none; align-items: center; justify-content: center; z-index: 50; }
#drop.on { display: flex; }
#drop div { border: 2px dashed rgba(255,255,255,.65); border-radius: 14px; padding: 34px 48px;
  font-size: 16px; text-align: center; }
footer { padding: 14px 22px 30px; color: var(--muted); font-size: 12px; }
"""

SCRIPT = r"""
const $ = (s) => document.querySelector(s);
let DATA = null, selected = null, degradedOnly = false, query = "", zoom = 1;

const SPEEDS = {1:"2.5 GT/s",2:"5 GT/s",3:"8 GT/s",4:"16 GT/s",5:"32 GT/s",6:"64 GT/s"};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const hex = (n, w) => n == null ? "—" : "0x" + n.toString(16).padStart(w, "0");
const bus2 = (n) => n.toString(16).padStart(2, "0");

function allNodes(d) {
  const out = [];
  const walk = (ns) => ns.forEach(n => { out.push(n); walk(n.children || []); });
  (d.domains || []).forEach(dom => { walk(dom.devices || []); walk(dom.orphans || []); });
  return out;
}

/* A node is shown when it matches, or when a descendant does: hiding a parent
   would detach a matching child from the branch that explains where it sits. */
function matches(n) {
  if (degradedOnly && !(n.link && n.link.degraded)) return false;
  if (!query) return true;
  return [n.address, n.display_name, n.class_name, n.port_type_name]
    .join(" ").toLowerCase().includes(query);
}
const subtreeMatches = (n) => matches(n) || (n.children || []).some(subtreeMatches);

function cardEl(n) {
  const card = document.createElement("div");
  card.className = "card" + (n.is_bridge ? " bridge" : "")
    + (n.link && n.link.degraded ? " degraded" : "")
    + (matches(n) ? "" : " dim");
  card.dataset.address = n.address;
  card.innerHTML =
    `<span class="addr">${esc(n.address)}</span>` +
    `<div class="nm" title="${esc(n.display_name)}">${esc(n.display_name)}</div>` +
    `<span class="role">${esc(n.port_type_name)}</span>` +
    (n.buses ? `<br><span class="bus">bus ${bus2(n.buses.secondary)}` +
       (n.buses.subordinate !== n.buses.secondary ? `–${bus2(n.buses.subordinate)}` : "") +
       `</span>` : "") +
    (n.link ? `<span class="lnk">${esc(n.link.text)}</span>` : "");
  card.onclick = (e) => { e.stopPropagation(); select(n, card); };
  return card;
}

/* One <li> per device: its card, then a <ul> of its children. The nesting is
   what the CSS draws the connectors from, so the markup IS the tree. */
function nodeEl(n) {
  const li = document.createElement("li");
  const holder = document.createElement("div");
  holder.className = "holder";
  holder.appendChild(cardEl(n));
  li.appendChild(holder);

  const kids = (n.children || []).filter(subtreeMatches);
  if (kids.length) {
    const twist = document.createElement("div");
    twist.className = "twist";
    twist.textContent = "−";
    twist.title = kids.length + " below";
    twist.onclick = (e) => {
      e.stopPropagation();
      const closed = li.classList.toggle("collapsed");
      twist.textContent = closed ? "+" + kids.length : "−";
    };
    holder.appendChild(twist);

    const branch = document.createElement("ul");
    branch.className = "branch";
    kids.forEach(k => branch.appendChild(nodeEl(k)));
    li.appendChild(branch);
  }
  return li;
}

/* The root complex is not a PCI function and has no configuration space, so it
   gets a card but no detail panel: there is nothing decoded to show. */
function rootCard(dom) {
  const card = document.createElement("div");
  card.className = "card rc";
  card.innerHTML =
    `<span class="addr">domain ${dom.domain.toString(16).padStart(4,"0")}</span>` +
    `<div class="nm">Root Complex</div>` +
    `<span class="role">presents bus ${bus2(dom.root_bus)}</span>`;
  return card;
}

function row(dt, dd) { return `<dt>${esc(dt)}</dt><dd>${dd}</dd>`; }

function select(n, el) {
  document.querySelectorAll(".card.sel").forEach(x => x.classList.remove("sel"));
  if (el) el.classList.add("sel");
  selected = n;
  const s = [`<h3>${esc(n.display_name)}</h3><div class="addr">${esc(n.address)}</div>`];

  s.push(`<div class="sect"><h4>Identity</h4><dl class="kv">` +
    row("Vendor", `${esc(n.vendor_name)} <span class="mono">${hex(n.vendor_id,4)}</span>`) +
    row("Device", `${esc(n.device_name)} <span class="mono">${hex(n.device_id,4)}</span>`) +
    row("Class", `${esc(n.class_name)} <span class="mono">${hex(n.class_code,6)}</span>`) +
    row("Revision", `<span class="mono">${hex(n.revision,2)}</span>`) +
    row("Header", n.is_bridge ? "Type 1 (bridge)" : "Type 0 (endpoint)") +
    row("Multifunction", n.multi_function ? "yes" : "no") +
    row("Port type", esc(n.port_type_name)) +
    (n.subsystem_vendor_id != null
      ? row("Subsystem", `<span class="mono">${hex(n.subsystem_vendor_id,4)}:${(n.subsystem_device_id||0).toString(16).padStart(4,"0")}</span>`)
      : "") +
    `</dl></div>`);

  if (n.buses) {
    s.push(`<div class="sect"><h4>Bus numbers (18h–1Ah)</h4><dl class="kv">` +
      row("Primary", `<span class="mono">${bus2(n.buses.primary)}</span>`) +
      row("Secondary", `<span class="mono">${bus2(n.buses.secondary)}</span>`) +
      row("Subordinate", `<span class="mono">${bus2(n.buses.subordinate)}</span>`) +
      row("Forwards", `buses ${bus2(n.buses.secondary)}–${bus2(n.buses.subordinate)}`) +
      `</dl></div>`);
  }

  if (n.link) {
    const l = n.link;
    s.push(`<div class="sect"><h4>Link</h4><dl class="kv">` +
      row("Current", `${esc(SPEEDS[l.current_speed_encoding] || "?")} x${l.current_width}`) +
      row("Maximum", `${esc(SPEEDS[l.max_speed_encoding] || "?")} x${l.max_width}`) +
      row("Status", l.degraded
          ? `<span style="color:var(--bad);font-weight:600">below maximum</span><br>${esc(l.degraded_reason||"")}`
          : (l.current_width === 0 ? "no link trained" : "at full speed and width")) +
      (l.link_active != null ? row("DLL active", l.link_active ? "yes" : "no") : "") +
      `</dl></div>`);
  }

  if (n.bars && n.bars.length) {
    s.push(`<div class="sect"><h4>BARs</h4><dl class="kv">` + n.bars.map(b =>
      row(`BAR${b.index} (${hex(b.offset,2)})`,
        `<span class="mono">${hex(b.base,0)}</span> · ${esc(b.space)}${b.is_64bit?" 64-bit":""}${b.prefetchable?" prefetchable":""}`)
    ).join("") + `</dl></div>`);
  }

  if (n.windows && n.windows.length) {
    s.push(`<div class="sect"><h4>Forwarding windows</h4><dl class="kv">` + n.windows.map(w =>
      row(w.kind, w.enabled ? `<span class="mono">${hex(w.base,0)}–${hex(w.limit,0)}</span>`
                            : `<span style="color:var(--muted)">disabled</span>`)
    ).join("") + `</dl></div>`);
  }

  if (n.capabilities && n.capabilities.length) {
    s.push(`<div class="sect"><h4>Capabilities</h4><dl class="kv">` +
      n.capabilities.map(c => row(hex(c.offset,2), esc(c.name))).join("") + `</dl></div>`);
  }
  if (n.extended_capabilities && n.extended_capabilities.length) {
    s.push(`<div class="sect"><h4>Extended capabilities</h4><dl class="kv">` +
      n.extended_capabilities.map(c => row(hex(c.offset,3), esc(c.name))).join("") + `</dl></div>`);
  }

  s.push(`<div class="sect"><h4>Read</h4><dl class="kv">` +
    row("Config bytes", n.config_bytes_read) + `</dl></div>`);

  if (n.warnings && n.warnings.length) {
    s.push(`<div class="sect"><h4>Mismatches</h4><div class="note warn">${n.warnings.map(esc).join("<br>")}</div></div>`);
  }
  $("#detail").innerHTML = s.join("");
}

function render() {
  const m = DATA.meta || {};
  $("#src").textContent = m.source || "";
  $("#gen").textContent = m.generated ? " · " + m.generated : "";
  $("#n-dev").textContent = m.device_count ?? allNodes(DATA).length;
  $("#n-br").textContent = m.bridge_count ?? "—";
  $("#n-deg").textContent = m.degraded_count ?? "—";
  $("#n-dom").textContent = (DATA.domains || []).length;

  const notes = [];
  if (m.access_note && m.access_note.length)
    notes.push(`<div class="note"><pre>${esc(m.access_note.join("\n"))}</pre></div>`);
  if (m.warnings && m.warnings.length)
    notes.push(`<div class="note warn"><pre>${esc(m.warnings.join("\n"))}</pre></div>`);
  $("#notes").innerHTML = notes.join("");

  const stage = $("#stage");
  stage.innerHTML = "";
  (DATA.domains || []).forEach(dom => {
    const box = document.createElement("div");
    box.className = "domain";
    const bus = bus2(dom.root_bus);
    box.innerHTML = `<h2>Domain ${dom.domain.toString(16).padStart(4,"0")} · root bus ${bus}</h2>`;

    const shown = (dom.devices || []).filter(subtreeMatches);
    const orphans = (dom.orphans || []).filter(subtreeMatches);
    if (!shown.length && !orphans.length) {
      box.innerHTML += `<div class="empty">Nothing matches the current filter.</div>`;
      stage.appendChild(box);
      return;
    }

    const top = document.createElement("ul");
    top.className = "branch top";
    const rootLi = document.createElement("li");
    const holder = document.createElement("div");
    holder.className = "holder";
    holder.appendChild(rootCard(dom));
    rootLi.appendChild(holder);

    const children = document.createElement("ul");
    children.className = "branch";
    shown.concat(orphans).forEach(n => children.appendChild(nodeEl(n)));
    rootLi.appendChild(children);
    top.appendChild(rootLi);
    box.appendChild(top);
    stage.appendChild(box);
  });

  if (selected) {
    const again = allNodes(DATA).find(n => n.address === selected.address);
    if (again) select(again, document.querySelector(`.card[data-address="${CSS.escape(again.address)}"]`));
  }
}

function setZoom(z) {
  zoom = Math.min(1.6, Math.max(0.35, z));
  $("#stage").style.zoom = zoom;
  $("#zval").textContent = Math.round(zoom * 100) + "%";
}

function load(data) {
  if (!data || !Array.isArray(data.domains)) throw new Error("not a pcitopo topology document");
  DATA = data; selected = null;
  $("#detail").innerHTML = `<div class="empty">Select a device to see everything decoded for it.</div>`;
  render();
}

/* --- drag and drop --- */
const drop = $("#drop");
let depth = 0;
["dragenter","dragover"].forEach(ev => document.addEventListener(ev, e => {
  e.preventDefault(); if (ev === "dragenter") depth++; drop.classList.add("on");
}));
["dragleave","drop"].forEach(ev => document.addEventListener(ev, e => {
  e.preventDefault();
  if (ev === "dragleave") { depth--; if (depth > 0) return; }
  depth = 0; drop.classList.remove("on");
}));
document.addEventListener("drop", e => { if (e.dataTransfer.files[0]) accept(e.dataTransfer.files[0]); });
$("#pick").onchange = e => { if (e.target.files[0]) accept(e.target.files[0]); };

async function accept(file) {
  const text = await file.text();
  try { load(JSON.parse(text)); return; } catch (_) { /* not our JSON: try the server */ }

  if (!SERVED) {
    $("#notes").innerHTML = `<div class="note warn">
      <b>${esc(file.name)}</b> is not a pcitopo JSON document, and this page is a standalone
      file with no decoder behind it — decoding configuration space in the browser would
      mean a second decoder that could disagree with the real one.
      <pre>Turn it into a page:   python3 -m pcitopo tree --lspci ${esc(file.name)} --html topo.html
Or drop it on a live one:  python3 -m pcitopo serve</pre></div>`;
    return;
  }
  $("#notes").innerHTML = `<div class="note">Decoding ${esc(file.name)}…</div>`;
  try {
    const res = await fetch("api/decode", { method: "POST", body: text,
      headers: { "Content-Type": "text/plain", "X-Filename": file.name } });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || res.statusText);
    load(body);
  } catch (err) {
    $("#notes").innerHTML = `<div class="note warn">Could not decode <b>${esc(file.name)}</b>: ${esc(err.message)}</div>`;
  }
}

/* --- toolbar --- */
$("#q").oninput = e => { query = e.target.value.trim().toLowerCase(); render(); };
$("#deg").onclick = () => { degradedOnly = !degradedOnly; $("#deg").classList.toggle("on", degradedOnly); render(); };
$("#zin").onclick = () => setZoom(zoom + 0.15);
$("#zout").onclick = () => setZoom(zoom - 0.15);
$("#zfit").onclick = () => {
  const stage = $("#stage"), view = $("#tree");
  setZoom(1);                       // measure unscaled, then scale to fit
  const wide = stage.scrollWidth;
  if (wide > 0) setZoom((view.clientWidth - 44) / wide);
};
$("#exp").onclick = () => {
  document.querySelectorAll("li.collapsed").forEach(li => li.classList.remove("collapsed"));
  document.querySelectorAll(".twist").forEach(t => t.textContent = "−");
};
$("#col").onclick = () => {
  document.querySelectorAll(".branch:not(.top) > li > .branch").forEach(b => {
    const li = b.parentElement;
    li.classList.add("collapsed");
    const t = li.querySelector(":scope > .holder > .twist");
    if (t) t.textContent = "+" + b.children.length;
  });
};
$("#rescan").onclick = async () => {
  $("#notes").innerHTML = `<div class="note">Re-scanning…</div>`;
  try { load(await (await fetch("api/topology")).json()); }
  catch (err) { $("#notes").innerHTML = `<div class="note warn">Re-scan failed: ${esc(err.message)}</div>`; }
};

load(EMBEDDED);
setZoom(1);
"""

BODY = """
<header>
  <h1>PCIe Topology</h1>
  <div class="sub"><span id="src"></span><span id="gen"></span></div>
  <div class="stats">
    <div class="stat"><b id="n-dev">0</b><span>functions</span></div>
    <div class="stat"><b id="n-br">0</b><span>bridges</span></div>
    <div class="stat"><b id="n-deg">0</b><span>below max</span></div>
    <div class="stat"><b id="n-dom">0</b><span>domains</span></div>
  </div>
</header>

<div class="toolbar">
  <input type="search" id="q" placeholder="Filter by address, name, class or port type&hellip;">
  <button id="deg">Below max only</button>
  <button id="exp">Expand all</button>
  <button id="col">Collapse all</button>
  <span class="zoom"><button id="zout">&minus;</button><span id="zval">100%</span><button id="zin">+</button><button id="zfit">Fit</button></span>
  __RESCAN__
  <button onclick="document.getElementById('pick').click()">Open file&hellip;</button>
  <input type="file" id="pick" hidden accept=".json,.txt,.log,text/plain,application/json">
</div>

<div id="notes"></div>

<main>
  <div id="tree"><div id="stage"></div></div>
  <aside id="detail"><div class="empty">Select a device to see everything decoded for it.</div></aside>
</main>

<footer>
  Every line in this tree is a Secondary Bus Number: a card sits under the bridge whose
  register 19h names its bus. Drag a <code>pcitopo --json</code> file onto this page to view it__DROPHINT__.
</footer>

<div id="drop"><div>Drop a topology file to view it</div></div>
"""

RESCAN_BUTTON = '<button id="rescan">Re-scan hardware</button>'


def render_page(payload: dict, served: bool = False) -> str:
    """The whole viewer as one string: markup, style, script and data.

    `served` switches on the two things that need a Python process behind them:
    the re-scan button and the decode-on-drop path. A standalone page keeps
    neither, and says so rather than offering a control that cannot work.
    """
    body = BODY.replace("__RESCAN__", RESCAN_BUTTON if served else "")
    body = body.replace(
        "__DROPHINT__",
        ", or an <code>lspci -vvv -xxxx</code> dump to have it decoded" if served else "",
    )

    # json.dumps produces valid JavaScript for this data, but "</script>" inside any
    # string would end the script element early. Escaping the slash keeps the JSON
    # identical to a parser and inert to the HTML tokenizer.
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")

    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{PAGE_TITLE}</title>\n"
        f"<style>{STYLE}</style>\n</head>\n<body>\n"
        f"{body}\n"
        "<script>\n"
        f"const SERVED = {'true' if served else 'false'};\n"
        f"const EMBEDDED = {data};\n"
        f"{SCRIPT}\n"
        "</script>\n</body>\n</html>\n"
    )
