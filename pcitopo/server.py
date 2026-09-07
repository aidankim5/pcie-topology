"""`pcitopo serve`: the viewer with a Python process behind it.

Standard library only, as everything here is: `http.server` is enough for a
tool that serves one page to one person on their own machine.

Three routes, and nothing else:

    GET  /              the viewer page, with the current scan baked in
    GET  /api/topology  a fresh scan as JSON -- what the Re-scan button calls
    POST /api/decode    a dropped file's text, decoded, as the same JSON

The POST route is the reason this mode exists. A standalone HTML file can only
re-draw a topology this tool already decoded; it cannot decode an
`lspci -vvv -xxxx` dump, because doing that in JavaScript would mean a second
decoder that could quietly disagree with the real one. With Python behind the
page, a dropped dump goes to `lspci.scan_from_lspci()` and comes back through
exactly the same decoders a live scan uses.

It binds to 127.0.0.1 by default, so nothing outside the machine can reach it.
That is deliberate: the response describes the machine's hardware in detail,
and this tool has no authentication and should never be the thing that needs
any. `--host` can override it for the case where the Linux box being inspected
is not the one with the browser, and the CLI says what that costs.
"""

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .ids import PciIds
from .lspci import NotAnLspciDump, scan_from_lspci
from .model import build_devices
from .sysfs import SysfsUnavailable, scan
from .topology import build_topology, cross_check
from .webui import build_payload, render_page

# A dropped file is read entirely into memory before parsing. `lspci -vvv -xxxx`
# for a large server is a few megabytes; this is generous and still bounded, so a
# mistaken upload cannot exhaust memory.
MAX_UPLOAD = 32 * 1024 * 1024


def payload_from_sysfs(sysfs_root: str, ids: PciIds) -> dict:
    """One complete scan of the live machine (or a captured tree), ready to serve."""
    result = scan(sysfs_root)
    devices = build_devices(result)
    topo = build_topology(devices)
    cross_check(topo, devices)
    return build_payload(topo, ids, result, devices, source=str(result.devices_dir))


def payload_from_lspci(text: str, ids: PciIds, source: str) -> dict:
    """A dropped `lspci` dump, decoded by the same code a live scan goes through."""
    result = scan_from_lspci(text, source=source)
    devices = build_devices(result)
    topo = build_topology(devices)
    cross_check(topo, devices)
    return build_payload(topo, ids, result, devices, source=f"{source} (lspci dump)")


class Handler(BaseHTTPRequestHandler):
    """The three routes. Configuration arrives as class attributes set by serve()."""

    sysfs_root = "/sys"
    ids = PciIds()
    quiet = False

    server_version = "pcitopo"
    sys_version = ""  # do not advertise the Python version

    def log_message(self, fmt, *args):  # noqa: A003 - the base class names it this
        if not self.quiet:
            super().log_message(fmt, *args)

    # --- helpers ---

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page is generated here and loads nothing from anywhere else, so the
        # strictest policy that still works is the correct one.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, doc: dict) -> None:
        self._send(code, json.dumps(doc).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    # --- routes ---

    def do_GET(self) -> None:  # noqa: N802 - the base class names it this
        route = self.path.split("?", 1)[0].rstrip("/") or "/"

        if route == "/":
            try:
                payload = payload_from_sysfs(self.sysfs_root, self.ids)
            except SysfsUnavailable as exc:
                # Serve the page anyway, with the failure written into it. A blank
                # browser tab would say less than the terminal already did.
                payload = {
                    "meta": {
                        "source": self.sysfs_root,
                        "summary": [f"Could not read {self.sysfs_root}: {exc}"],
                        "access_note": [],
                        "warnings": [
                            "No PCI devices could be read. On Linux, run this on the machine "
                            "itself; anywhere else, drop an lspci dump onto the page."
                        ],
                        "device_count": 0,
                        "bridge_count": 0,
                        "degraded_count": 0,
                    },
                    "domains": [],
                }
            page = render_page(payload, served=True)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return

        if route == "/api/topology":
            try:
                self._json(200, payload_from_sysfs(self.sysfs_root, self.ids))
            except SysfsUnavailable as exc:
                self._error(503, str(exc))
            return

        self._error(404, f"no route {route}")

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0].rstrip("/")
        if route != "/api/decode":
            self._error(404, f"no route {route}")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "Content-Length is not a number")
            return
        if length <= 0:
            self._error(400, "the dropped file was empty")
            return
        if length > MAX_UPLOAD:
            self._error(413, f"that file is larger than the {MAX_UPLOAD // (1024 * 1024)} MB limit")
            return

        raw = self.rfile.read(length)
        # A dump is ASCII in practice; replace rather than fail on a stray byte, since
        # a single bad character should not lose an otherwise readable capture.
        text = raw.decode("utf-8", errors="replace")
        name = self.headers.get("X-Filename", "dropped file")

        # Our own JSON first: it is the cheaper answer and the more common drop.
        try:
            doc = json.loads(text)
            if isinstance(doc, dict) and isinstance(doc.get("domains"), list):
                self._json(200, doc)
                return
        except json.JSONDecodeError:
            pass

        try:
            self._json(200, payload_from_lspci(text, self.ids, source=name))
        except NotAnLspciDump as exc:
            self._error(422, str(exc))
        except Exception as exc:  # noqa: BLE001 - the browser needs a reason, not a traceback
            self._error(500, f"{type(exc).__name__}: {exc}")


def serve(
    sysfs_root: str = "/sys",
    ids: PciIds | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    quiet: bool = False,
) -> int:
    """Run the viewer until interrupted. Returns the process exit code."""
    Handler.sysfs_root = sysfs_root
    Handler.ids = ids if ids is not None else PciIds.load()
    Handler.quiet = quiet

    # port 0 asks the OS for any free port, which is what makes the tool usable
    # when something else already holds the default.
    httpd = ThreadingHTTPServer((host, port), Handler)
    actual = httpd.server_address[1]
    url = f"http://{host}:{actual}/"

    print(f"pcitopo: serving the viewer at {url}")
    print(f"pcitopo: reading {sysfs_root}. Drop an lspci dump on the page to view another machine.")
    print("pcitopo: press Ctrl+C to stop.")
    if open_browser:
        # A failure here is not worth aborting for: the URL is printed above.
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\npcitopo: stopped.")
    finally:
        httpd.server_close()
    return 0


def write_html(payload: dict, path: str) -> None:
    """The standalone page: one file, no server, opens anywhere."""
    Path(path).write_text(render_page(payload, served=False), encoding="utf-8")
