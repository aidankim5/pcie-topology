"""The command line, end to end: what each verb prints and what it exits with.

These run `main()` in-process with stdout captured, so they exercise the real
argparse configuration and the real renderers rather than a stand-in.
"""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from pcitopo.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
FULL = str(FIXTURES / "sysfs-capture-raptorlake")
NOROOT = str(FIXTURES / "sysfs-capture-raptorlake-noroot")


def run(*argv) -> tuple[int, str, str]:
    """main(argv) with its output captured. Returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestList(unittest.TestCase):
    def test_exits_zero_and_lists_every_function(self):
        code, out, _ = run("list", "--sysfs-root", FULL)
        self.assertEqual(code, 0)
        self.assertIn("Read 27 PCI functions", out)

    def test_names_the_gpu(self):
        _, out, _ = run("list", "--sysfs-root", FULL)
        self.assertIn("10de:2489", out)

    def test_marks_multifunction_devices(self):
        _, out, _ = run("list", "--sysfs-root", FULL)
        self.assertIn("T0+MF", out)
        self.assertIn("T1+MF", out)


class TestBuses(unittest.TestCase):
    def test_prints_the_bus_number_table(self):
        code, out, _ = run("buses", "--sysfs-root", FULL)
        self.assertEqual(code, 0)
        self.assertIn("Pri", out)
        self.assertIn("Sec", out)
        self.assertIn("Sub", out)

    def test_real_hardware_passes_every_check(self):
        _, out, _ = run("buses", "--sysfs-root", FULL)
        self.assertIn("All checks pass", out)

    def test_marks_the_empty_slots(self):
        _, out, _ = run("buses", "--sysfs-root", FULL)
        self.assertIn("empty slot", out)

    def test_works_without_root(self):
        # The bus numbers are inside the 64 bytes, so this verb never needs sudo.
        code, out, _ = run("buses", "--sysfs-root", NOROOT)
        self.assertEqual(code, 0)
        self.assertIn("All checks pass", out)


class TestTree(unittest.TestCase):
    def test_draws_a_tree(self):
        code, out, _ = run("tree", "--sysfs-root", FULL)
        self.assertEqual(code, 0)
        self.assertIn("Domain 0000", out)
        self.assertIn("0000:01:00.0", out)

    def test_annotates_links(self):
        _, out, _ = run("tree", "--sysfs-root", FULL)
        self.assertIn("16 GT/s x4 (max 16 GT/s x4)", out)

    def test_flags_a_degraded_link_inline(self):
        _, out, _ = run("tree", "--sysfs-root", FULL)
        self.assertIn("DEGRADED", out)

    def test_verbose_adds_bars_and_capabilities(self):
        _, out, _ = run("tree", "-v", "--sysfs-root", FULL)
        self.assertIn("BAR0", out)
        self.assertIn("capabilities:", out)
        self.assertIn("window", out)

    def test_unprivileged_run_says_what_is_missing(self):
        _, out, _ = run("tree", "--sysfs-root", NOROOT)
        self.assertIn("Re-run with sudo", out)
        self.assertNotIn("DEGRADED", out)

    def test_unprivileged_run_still_draws_the_whole_tree(self):
        _, privileged, _ = run("tree", "--sysfs-root", FULL)
        _, limited, _ = run("tree", "--sysfs-root", NOROOT)
        for address in ("0000:01:00.0", "0000:02:00.0", "0000:06:00.0"):
            self.assertIn(address, limited)
            self.assertIn(address, privileged)


class TestDegradedFlag(unittest.TestCase):
    def test_lists_only_degraded_links(self):
        code, out, _ = run("tree", "--degraded", "--sysfs-root", FULL)
        self.assertEqual(code, 0)
        self.assertIn("0000:01:00.0", out)
        self.assertNotIn("0000:02:00.0", out)  # the NVMe is at full rate

    def test_says_nothing_is_degraded_when_nothing_is(self):
        _, out, _ = run("tree", "--degraded", "--sysfs-root", NOROOT)
        self.assertIn("No degraded links", out)


class TestJsonOutput(unittest.TestCase):
    def test_emits_parseable_json(self):
        code, out, _ = run("tree", "--json", "--sysfs-root", FULL)
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(len(doc["domains"]), 1)

    def test_json_only_no_tree_drawing(self):
        _, out, _ = run("tree", "--json", "--sysfs-root", FULL)
        self.assertNotIn("Domain 0000  (root bus", out)


class TestDotOutput(unittest.TestCase):
    def test_writes_a_file(self):
        with TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "topo.dot")
            code, out, _ = run("tree", "--dot", path, "--sysfs-root", FULL)
            self.assertEqual(code, 0)
            self.assertIn("Wrote", out)
            self.assertTrue(Path(path).read_text().startswith("digraph pcie {"))

    def test_dash_writes_to_stdout(self):
        code, out, _ = run("tree", "--dot", "-", "--sysfs-root", FULL)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("digraph pcie {"))


class TestFailureModes(unittest.TestCase):
    def test_missing_sysfs_root_exits_one(self):
        code, _, err = run("list", "--sysfs-root", "/nonexistent-path-for-tests")
        self.assertEqual(code, 1)
        self.assertIn("pcitopo:", err)

    def test_unknown_command_is_an_argparse_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            run("nonsense")
        self.assertEqual(caught.exception.code, 2)

    def test_no_command_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            run()
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
