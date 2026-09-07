"""The visual tool: the generated page, and the server behind the served variant.

The page is generated text, so these tests check the properties that would
break it silently: that it is self-contained, that its data survives being
embedded in a <script> element, and that the standalone and served variants
differ in exactly the ways they are meant to.

The server is exercised over a real socket on a random port rather than by
calling handler methods directly, because the routing and the status codes are
the part worth testing.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from pcitopo.ids import PciIds
from pcitopo.model import build_devices
from pcitopo.server import Handler, payload_from_lspci, payload_from_sysfs
from pcitopo.sysfs import scan
from pcitopo.topology import build_topology, cross_check
from pcitopo.webui import build_payload, render_page

FIXTURES = Path(__file__).parent / "fixtures"
FULL = str(FIXTURES / "sysfs-capture-raptorlake")
NOROOT = str(FIXTURES / "sysfs-capture-raptorlake-noroot")
DUMP = FIXTURES / "dumps" / "lspci-full-capture.txt"

DEVICE_COUNT = 27


def payload(root: str = FULL) -> dict:
    result = scan(root)
    devices = build_devices(result)
    topo = build_topology(devices)
    cross_check(topo, devices)
    return build_payload(topo, PciIds.parse(""), result, devices, source=root)


class TestPayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = payload()

    def test_counts_match_the_scan(self):
        self.assertEqual(self.doc["meta"]["device_count"], DEVICE_COUNT)
        self.assertEqual(self.doc["meta"]["bridge_count"], 7)
        self.assertEqual(self.doc["meta"]["degraded_count"], 5)

    def test_carries_the_access_note_so_the_viewer_cannot_hide_it(self):
        # An unprivileged scan drawn without its note would read as the whole picture.
        limited = payload(NOROOT)
        self.assertTrue(limited["meta"]["access_note"])
        self.assertIn("sudo", " ".join(limited["meta"]["access_note"]))

    def test_tree_shape_is_preserved(self):
        port = next(d for d in self.doc["domains"][0]["devices"] if d["address"] == "0000:00:01.0")
        self.assertEqual(
            sorted(c["address"] for c in port["children"]),
            ["0000:01:00.0", "0000:01:00.1"],
        )

    def test_every_device_has_a_display_name(self):
        def walk(nodes):
            for n in nodes:
                yield n
                yield from walk(n["children"])

        for node in walk(self.doc["domains"][0]["devices"]):
            with self.subTest(address=node["address"]):
                self.assertTrue(node["display_name"])

    def test_the_payload_is_json_serialisable(self):
        # It is embedded in a <script> element, so a non-serialisable value here
        # would produce a page that loads and then does nothing.
        json.dumps(self.doc)


class TestPageGeneration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = payload()
        cls.standalone = render_page(cls.doc, served=False)
        cls.served = render_page(cls.doc, served=True)

    def test_is_a_complete_html_document(self):
        self.assertTrue(self.standalone.startswith("<!doctype html>"))
        self.assertIn("<title>", self.standalone)
        self.assertTrue(self.standalone.rstrip().endswith("</html>"))

    def test_loads_nothing_from_the_network(self):
        # The pure-stdlib rule applied to the front end: no CDN, no fonts, no fetch
        # of anything the file does not already contain.
        for marker in ("http://", "https://", "//cdn", "<link"):
            self.assertNotIn(marker, self.standalone)

    def test_the_data_is_embedded(self):
        self.assertIn("const EMBEDDED = {", self.standalone)
        self.assertIn("0000:01:00.0", self.standalone)

    def test_standalone_has_no_controls_that_need_a_server(self):
        self.assertIn("const SERVED = false;", self.standalone)
        self.assertNotIn('id="rescan"', self.standalone)

    def test_served_page_enables_them(self):
        self.assertIn("const SERVED = true;", self.served)
        self.assertIn('id="rescan"', self.served)

    def test_closing_script_tag_inside_data_cannot_end_the_script(self):
        # A device name containing "</script>" would otherwise close the element
        # early and leave the rest of the page as visible text.
        doc = payload()
        doc["meta"]["source"] = "</script><h1>injected</h1>"
        page = render_page(doc)
        self.assertNotIn("</script><h1>injected", page)
        self.assertIn("<\\/script>", page)

    def test_the_embedded_json_still_parses_after_escaping(self):
        doc = payload()
        doc["meta"]["source"] = 'a </script> and a "quote"'
        page = render_page(doc)
        start = page.index("const EMBEDDED = ") + len("const EMBEDDED = ")
        end = page.index(";\n", start)
        parsed = json.loads(page[start:end].replace("<\\/", "</"))
        self.assertEqual(parsed["meta"]["source"], 'a </script> and a "quote"')


class TestFamilyTreeLayout(unittest.TestCase):
    """The drawing is a nested <ul>/<li>, and the connectors are CSS on it.

    These are string checks against generated text, which is shallow, but each
    one pins a rule that was wrong at some point and would be wrong silently:
    a stem drawn above the top card, or a scrollbar that lies about the width.
    """

    @classmethod
    def setUpClass(cls):
        cls.page = render_page(payload(), served=False)

    def test_the_top_card_has_no_connector_above_it(self):
        # The root complex is an only-child <li>, so without this rule it inherits
        # the only-child stem and grows a line out of its top into empty space.
        self.assertIn(".branch.top > li::before, .branch.top > li::after { display: none; }", self.page)

    def test_the_last_child_keeps_its_stem_but_drops_the_bar(self):
        # Removing the whole border would take the vertical stem with it and leave
        # the rightmost card floating unattached.
        self.assertIn(".branch > li:last-child::after { border-top: 0; }", self.page)

    def test_an_only_child_gets_a_straight_stem_not_a_bar(self):
        self.assertIn(".branch > li:only-child::before { display: none; }", self.page)

    def test_collapsing_hides_a_whole_subtree(self):
        self.assertIn("li.collapsed > .branch { display: none; }", self.page)

    def test_zoom_reflows_rather_than_repainting(self):
        # A CSS transform is applied after layout, so the scroll area would keep the
        # unscaled width and the scrollbars would not match what is drawn. The
        # assertion is on the script, not the stylesheet: the stylesheet mentions
        # transforms in a comment and in unrelated rules (.twist centres itself).
        self.assertIn("#stage { zoom: 1; }", self.page)
        self.assertIn('$("#stage").style.zoom = zoom;', self.page)
        self.assertNotIn('$("#stage").style.transform', self.page)

    def test_the_script_builds_nested_lists(self):
        self.assertIn('branch.className = "branch"', self.page)
        self.assertIn('createElement("li")', self.page)


class TestServer(unittest.TestCase):
    """Real requests over a real socket, on a port the OS picks."""

    @classmethod
    def setUpClass(cls):
        Handler.sysfs_root = FULL
        Handler.ids = PciIds.parse("")
        Handler.quiet = True
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path: str):
        return urllib.request.urlopen(self.base + path, timeout=10)

    def post(self, path: str, data: bytes, filename: str = "dropped.txt"):
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "text/plain", "X-Filename": filename},
        )
        return urllib.request.urlopen(req, timeout=30)

    def test_root_serves_the_viewer(self):
        with self.get("/") as response:
            body = response.read().decode("utf-8")
        self.assertIn("<!doctype html>", body)
        self.assertIn("const SERVED = true;", body)

    def test_topology_endpoint_returns_the_scan(self):
        with self.get("/api/topology") as response:
            doc = json.loads(response.read())
        self.assertEqual(doc["meta"]["device_count"], DEVICE_COUNT)

    def test_dropping_an_lspci_dump_decodes_it(self):
        with self.post("/api/decode", DUMP.read_bytes(), "lspci-full.txt") as response:
            doc = json.loads(response.read())
        self.assertEqual(doc["meta"]["device_count"], DEVICE_COUNT)
        self.assertIn("lspci dump", doc["meta"]["source"])

    def test_dropping_our_own_json_is_returned_as_is(self):
        original = json.dumps(payload()).encode("utf-8")
        with self.post("/api/decode", original, "topo.json") as response:
            doc = json.loads(response.read())
        self.assertEqual(doc["meta"]["device_count"], DEVICE_COUNT)

    def test_dropping_something_else_is_a_422_with_a_reason(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/decode", b"just some notes\n", "notes.txt")
        # An HTTPError is itself a response object and holds an open socket, so it
        # is closed here rather than left to the garbage collector.
        with caught.exception as err:
            self.assertEqual(err.code, 422)
            self.assertIn("lspci", json.loads(err.read())["error"])

    def test_an_empty_drop_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/decode", b"", "empty.txt")
        with caught.exception as err:
            self.assertEqual(err.code, 400)

    def test_unknown_routes_are_404(self):
        for path in ("/nope", "/api/nope"):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.get(path)
                with caught.exception as err:
                    self.assertEqual(err.code, 404)

    def test_posting_to_an_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/api/other", b"x")
        with caught.exception as err:
            self.assertEqual(err.code, 404)

    def test_the_page_declares_a_restrictive_content_security_policy(self):
        with self.get("/") as response:
            csp = response.headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)


class TestServerPayloadHelpers(unittest.TestCase):
    def test_sysfs_payload(self):
        doc = payload_from_sysfs(FULL, PciIds.parse(""))
        self.assertEqual(doc["meta"]["device_count"], DEVICE_COUNT)

    def test_lspci_payload(self):
        doc = payload_from_lspci(DUMP.read_text(errors="replace"), PciIds.parse(""), "d.txt")
        self.assertEqual(doc["meta"]["device_count"], DEVICE_COUNT)

    def test_both_sources_produce_the_same_tree(self):
        a = payload_from_sysfs(FULL, PciIds.parse(""))
        b = payload_from_lspci(DUMP.read_text(errors="replace"), PciIds.parse(""), "d.txt")

        def shape(doc):
            def walk(nodes):
                return sorted(
                    (n["address"], tuple(sorted(c["address"] for c in n["children"])))
                    for n in nodes
                    for _ in [0]
                ) + [x for n in nodes for x in walk(n["children"])]

            return sorted(walk(doc["domains"][0]["devices"]))

        self.assertEqual(shape(a), shape(b))


if __name__ == "__main__":
    unittest.main()
