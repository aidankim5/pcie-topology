"""The visual tool: one self-contained HTML page, built from the same model objects.

Like render.py and export.py, nothing in here decodes anything. It is a third
renderer over the same `Topology`, which is why adding it changed no decoder.

The page is a single file with the data, the stylesheet and the script all
inlined. No CDN, no build step, no server required: `--html topo.html` writes
it and any browser opens it, on Linux or Windows or a phone. That constraint is
the same pure-stdlib rule the rest of the repo follows, applied to the front end.

Two ways it gets its data, and the difference matters:

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
  --bg: #f6f7f9; --panel: #ffffff; --ink: #14171a; --muted: #5b6570;
  --line: #dfe3e8; --accent: #2b6cb0; --accent-soft: #e8f0f8;
  --bad: #b03030; --bad-soft: #fbeaea; --shadow: rgba(15,20,25,.08);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171a; --panel: #1c2024; --ink: #e6e9ec; --muted: #94a0ac;
    --line: #2c3238; --accent: #6aa9e0; --accent-soft: #1e2a36;
    --bad: #e8807a; --bad-soft: #3a2222; --shadow: rgba(0,0,0,.35);
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
code, .mono, .addr { font-family: ui-monospace, "Cascadia Code", Consolas, "DejaVu Sans Mono", monospace; }

header { padding: 18px 22px 12px; border-bottom: 1px solid var(--line); background: var(--panel); }
h1 { margin: 0 0 2px; font-size: 17px; font-weight: 640; letter-spacing: -.01em; }
.sub { color: var(--muted); font-size: 12.5px; }
.stats { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 10px; }
.stat { background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 6px 11px; }
.stat b { font-size: 16px; font-weight: 640; display: block; line-height: 1.2; }
.stat span { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }

.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px 22px;
  border-bottom: 1px solid var(--line); background: var(--panel); position: sticky; top: 0; z-index: 5; }
input[type=search] { flex: 1 1 220px; min-width: 160px; padding: 6px 10px; border-radius: 7px;
  border: 1px solid var(--line); background: var(--bg); color: var(--ink); font-size: 13px; }
button { padding: 6px 11px; border-radius: 7px; border: 1px solid var(--line);
  background: var(--bg); color: var(--ink); font-size: 12.5px; cursor: pointer; }
button:hover { border-color: var(--accent); color: var(--accent); }
button.on { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }

main { display: grid; grid-template-columns: minmax(0,1fr) 340px; gap: 0; align-items: start; }
@media (max-width: 900px) { main { grid-template-columns: 1fr; } #detail { border-left: none; border-top: 1px solid var(--line); } }

#tree { padding: 14px 22px 60px; overflow-x: auto; }
.domain { margin-bottom: 22px; }
.domain > h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted); margin: 0 0 8px; font-weight: 620; }

.node { border: 1px solid transparent; border-radius: 9px; padding: 7px 10px; margin: 2px 0;
  cursor: pointer; display: flex; gap: 9px; align-items: baseline; flex-wrap: wrap;
  transition: background .12s, border-color .12s; }
.node:hover { background: var(--panel); border-color: var(--line); }
.node.sel { background: var(--accent-soft); border-color: var(--accent); }
.node.dim { opacity: .3; }
.kids { margin-left: 20px; padding-left: 14px; border-left: 1px dashed var(--line); }

.addr { font-size: 12.5px; color: var(--accent); font-weight: 600; }
.nm { flex: 1 1 240px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.badge { font-size: 10.5px; padding: 1.5px 7px; border-radius: 20px; border: 1px solid var(--line);
  color: var(--muted); white-space: nowrap; }
.badge.port { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
.badge.bad { background: var(--bad-soft); border-color: var(--bad); color: var(--bad); }
.link { font-size: 12px; color: var(--muted); white-space: nowrap; }
.link.bad { color: var(--bad); font-weight: 600; }
.twist { width: 14px; color: var(--muted); user-select: none; font-size: 11px; }

#detail { position: sticky; top: 49px; max-height: calc(100vh - 49px); overflow-y: auto;
  border-left: 1px solid var(--line); background: var(--panel); padding: 16px 18px 40px; }
#detail h3 { margin: 0 0 3px; font-size: 14px; }
#detail .addr { font-size: 13px; }
.sect { margin-top: 16px; }
.sect h4 { margin: 0 0 6px; font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); font-weight: 620; }
.kv { display: grid; grid-template-columns: 116px minmax(0,1fr); gap: 3px 10px; font-size: 12.5px; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; word-break: break-word; }
.empty { color: var(--muted); font-size: 13px; }

.note { margin: 0 22px 14px; padding: 10px 13px; border-radius: 9px; font-size: 12.5px;
  background: var(--panel); border: 1px solid var(--line); color: var(--muted); }
.note.warn { border-color: var(--bad); background: var(--bad-soft); color: var(--bad); }
.note pre { margin: 6px 0 0; white-space: pre-wrap; font-size: 12px; }

#drop { position: fixed; inset: 0; background: rgba(20,30,45,.72); color: #fff;
  display: none; align-items: center; justify-content: center; z-index: 50; backdrop-filter: blur(2px); }
#drop.on { display: flex; }
#drop div { border: 2px dashed rgba(255,255,255,.65); border-radius: 14px; padding: 34px 48px;
  font-size: 16px; text-align: center; }
footer { padding: 14px 22px 30px; color: var(--muted); font-size: 12px; }
"""

SCRIPT = r"""
const $ = (s) => document.querySelector(s);
let DATA = null, selected = null, degradedOnly = false, query = "";

const SPEEDS = {1:"2.5 GT/s",2:"5 GT/s",3:"8 GT/s",4:"16 GT/s",5:"32 GT/s",6:"64 GT/s"};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const hex = (n, w) => n == null ? "—" : "0x" + n.toString(16).padStart(w, "0");

function allNodes(d) {
  const out = [];
  const walk = (ns) => ns.forEach(n => { out.push(n); walk(n.children || []); });
  (d.domains || []).forEach(dom => { walk(dom.devices || []); walk(dom.orphans || []); });
  return out;
}

/* A node is shown when it matches, or when a descendant does: hiding a parent
   would detach a matching child from the tree that explains where it sits. */
function matches(n) {
  if (degradedOnly && !(n.link && n.link.degraded)) return false;
  if (!query) return true;
  const hay = [n.address, n.display_name, n.class_name, n.port_type_name].join(" ").toLowerCase();
  return hay.includes(query);
}
function subtreeMatches(n) {
  return matches(n) || (n.children || []).some(subtreeMatches);
}

function nodeEl(n) {
  const wrap = document.createElement("div");
  const row = document.createElement("div");
  row.className = "node";
  row.dataset.address = n.address;
  const kids = (n.children || []).filter(subtreeMatches);
  const twist = kids.length ? "▾" : "";
  const linkCls = n.link && n.link.degraded ? "link bad" : "link";
  const linkTxt = n.link ? esc(n.link.text) : "";
  const badges = [`<span class="badge port">${esc(n.port_type_name)}</span>`];
  if (n.buses) badges.push(`<span class="badge">bus ${n.buses.secondary.toString(16).padStart(2,"0")}</span>`);
  if (n.link && n.link.degraded) badges.push(`<span class="badge bad">degraded</span>`);
  row.innerHTML =
    `<span class="twist">${twist}</span>` +
    `<span class="addr">${esc(n.address)}</span>` +
    badges.join("") +
    `<span class="nm">${esc(n.display_name)}</span>` +
    `<span class="${linkCls}">${linkTxt}</span>`;
  if (!matches(n)) row.classList.add("dim");
  row.onclick = (e) => { e.stopPropagation(); select(n, row); };
  wrap.appendChild(row);

  if (kids.length) {
    const box = document.createElement("div");
    box.className = "kids";
    kids.forEach(k => box.appendChild(nodeEl(k)));
    wrap.appendChild(box);
    row.querySelector(".twist").onclick = (e) => {
      e.stopPropagation();
      const hidden = box.style.display === "none";
      box.style.display = hidden ? "" : "none";
      row.querySelector(".twist").textContent = hidden ? "▾" : "▸";
    };
  }
  return wrap;
}

function row(dt, dd) { return `<dt>${esc(dt)}</dt><dd>${dd}</dd>`; }

function select(n, el) {
  document.querySelectorAll(".node.sel").forEach(x => x.classList.remove("sel"));
  if (el) el.classList.add("sel");
  selected = n;
  const s = [];
  s.push(`<h3>${esc(n.display_name)}</h3><div class="addr">${esc(n.address)}</div>`);

  s.push(`<div class="sect"><h4>Identity</h4><dl class="kv">` +
    row("Vendor", `${esc(n.vendor_name)} <span class="mono">${hex(n.vendor_id,4)}</span>`) +
    row("Device", `${esc(n.device_name)} <span class="mono">${hex(n.device_id,4)}</span>`) +
    row("Class", `${esc(n.class_name)} <span class="mono">${hex(n.class_code,6)}</span>`) +
    row("Revision", `<span class="mono">${hex(n.revision,2)}</span>`) +
    row("Header", n.is_bridge ? "Type 1 (bridge)" : "Type 0 (endpoint)" + (n.multi_function ? " +MF" : "")) +
    row("Port type", esc(n.port_type_name)) +
    (n.subsystem_vendor_id != null
      ? row("Subsystem", `<span class="mono">${hex(n.subsystem_vendor_id,4)}:${(n.subsystem_device_id||0).toString(16).padStart(4,"0")}</span>`)
      : "") +
    `</dl></div>`);

  if (n.buses) {
    s.push(`<div class="sect"><h4>Bus numbers (18h–1Ah)</h4><dl class="kv">` +
      row("Primary", `<span class="mono">${n.buses.primary.toString(16).padStart(2,"0")}</span>`) +
      row("Secondary", `<span class="mono">${n.buses.secondary.toString(16).padStart(2,"0")}</span>`) +
      row("Subordinate", `<span class="mono">${n.buses.subordinate.toString(16).padStart(2,"0")}</span>`) +
      row("Claims", `buses ${n.buses.secondary.toString(16).padStart(2,"0")}–${n.buses.subordinate.toString(16).padStart(2,"0")}`) +
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
      row(w.kind, w.enabled
          ? `<span class="mono">${hex(w.base,0)}–${hex(w.limit,0)}</span>`
          : `<span style="color:var(--muted)">disabled</span>`)
    ).join("") + `</dl></div>`);
  }

  if (n.capabilities && n.capabilities.length) {
    s.push(`<div class="sect"><h4>Capabilities</h4><dl class="kv">` + n.capabilities.map(c =>
      row(hex(c.offset,2), esc(c.name))).join("") + `</dl></div>`);
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
  if (m.access_note && m.access_note.length) {
    notes.push(`<div class="note"><pre>${esc(m.access_note.join("\n"))}</pre></div>`);
  }
  if (m.warnings && m.warnings.length) {
    notes.push(`<div class="note warn"><pre>${esc(m.warnings.join("\n"))}</pre></div>`);
  }
  $("#notes").innerHTML = notes.join("");

  const host = $("#tree");
  host.innerHTML = "";
  (DATA.domains || []).forEach(dom => {
    const box = document.createElement("div");
    box.className = "domain";
    const bus = dom.root_bus.toString(16).padStart(2, "0");
    box.innerHTML = `<h2>Domain ${dom.domain.toString(16).padStart(4,"0")} · root bus ${bus}</h2>`;
    const shown = (dom.devices || []).filter(subtreeMatches);
    shown.forEach(n => box.appendChild(nodeEl(n)));
    (dom.orphans || []).filter(subtreeMatches).forEach(n => box.appendChild(nodeEl(n)));
    if (!shown.length && !(dom.orphans || []).length) {
      box.innerHTML += `<div class="empty">Nothing matches the current filter.</div>`;
    }
    host.appendChild(box);
  });

  if (selected) {
    const again = allNodes(DATA).find(n => n.address === selected.address);
    if (again) select(again, document.querySelector(`.node[data-address="${CSS.escape(again.address)}"]`));
  }
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
      file, so it has no decoder behind it — decoding configuration space in the browser
      would mean a second decoder that could disagree with the real one.
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
$("#exp").onclick = () => {
  document.querySelectorAll(".kids").forEach(k => k.style.display = "");
  document.querySelectorAll(".twist").forEach(t => { if (t.textContent) t.textContent = "▾"; });
};
$("#col").onclick = () => {
  document.querySelectorAll(".kids").forEach(k => k.style.display = "none");
  document.querySelectorAll(".twist").forEach(t => { if (t.textContent) t.textContent = "▸"; });
};
$("#rescan").onclick = async () => {
  $("#notes").innerHTML = `<div class="note">Re-scanning…</div>`;
  try { load(await (await fetch("api/topology")).json()); }
  catch (err) { $("#notes").innerHTML = `<div class="note warn">Re-scan failed: ${esc(err.message)}</div>`; }
};

load(EMBEDDED);
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
  <input type="search" id="q" placeholder="Filter by address, name, class or port type…">
  <button id="deg">Below max only</button>
  <button id="exp">Expand all</button>
  <button id="col">Collapse all</button>
  __RESCAN__
  <label class="btn"><button onclick="document.getElementById('pick').click()">Open file…</button></label>
  <input type="file" id="pick" hidden accept=".json,.txt,.log,text/plain,application/json">
</div>

<div id="notes"></div>

<main>
  <div id="tree"></div>
  <aside id="detail"><div class="empty">Select a device to see everything decoded for it.</div></aside>
</main>

<footer>
  Drag a <code>pcitopo --json</code> file onto this page to view it__DROPHINT__.
  Built by <code>pcitopo</code> &mdash; the tree comes from Type&nbsp;1 bridge registers,
  never from guesswork.
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
