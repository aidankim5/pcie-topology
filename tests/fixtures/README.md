# Fixtures: what is real and what is not

Tests must run with no PCI hardware present, so they read a directory that has
the same shape as `/sys` and the same bytes in it. Nothing in `pcitopo/sysfs.py`
does anything cleverer than listing a directory and opening the files in it, so
a fixture tree exercises the real code path.

There are two generations of fixture here. The **capture** trees are a real
machine, recorded with root, and are what the substantive tests assert against.
The **synthesized** trees came first, before that capture existed, and are kept
because they cover shapes the real machine does not have.

## `sysfs-capture-raptorlake/` and `-noroot/` — a real machine

Built by `python tools/import_capture.py PATH-TO-CAPTURE`. Do not edit by hand.

Every byte in `sysfs-capture-raptorlake/` came off live hardware: 27 functions
of a Raptor Lake desktop, captured with `sudo` from a Ubuntu live USB, so the
`config` files hold 256 or 4096 bytes rather than the 64 an unprivileged read
returns. That is what makes the link-speed tests possible at all.

`sysfs-capture-raptorlake-noroot/` is the **same capture** with every `config`
file truncated to 64 bytes. It is not a separate recording and not a
simulation: a non-root read returns exactly the first 64 bytes, so truncating
reproduces the unprivileged path byte for byte.

Having both is what lets the tests prove, rather than assume, the claim the
tool is organised around:

- identity and the whole tree survive the 64-byte read, unchanged;
- the capability chain, and with it link speed and width, does not.

Deliberate content worth knowing about, all of it real:

| Address | What makes it interesting |
| --- | --- |
| `00:01.0` | Gen5 x16 root port running at 2.5 GT/s — the idle GPU below it |
| `01:00.0`, `01:00.1` | two functions of one card: siblings, not parent and child |
| `00:1b.0`, `00:1d.0` | empty slots: a bus number assigned, width 0, no link |
| `00:1c.3` | a Gen3 port with a Gen2 device in it |
| `00:0a.0`, `00:0e.0` | Root Complex Integrated Endpoints: no link registers at all |

## `dumps/` — captured from hardware

| File | What it is |
| --- | --- |
| `lspci-tv-capture.txt` | `lspci -tv` from the same boot as the capture above: the topology, as the system's own tool sees it |
| `lspci-nn-capture.txt` | `lspci -nn` from the same boot: every function's identity |
| `rtx3060ti_01-00.0.txt` | `sudo lspci -vvv -xxxx -s 01:00.0`, 4096 bytes of real configuration space from an NVIDIA RTX 3060 Ti |
| `samsung990pro_02-00.0.txt` | the same, for a Samsung 990 PRO NVMe SSD |
| `lspci-tv-raptorlake.txt` | an earlier `lspci -tv` from the same machine |

The two `-capture` files are the **answer key**. `tests/test_capture.py` parses
them and compares field by field: vendor, device, class and revision against
`lspci -nn`, and every bridge's secondary and subordinate bus number against
`lspci -tv`. Because those were produced by a different tool in the same boot,
a decoder that is self-consistent and wrong fails them.

The two `.txt` dumps came from the `pcie-config-decoder` repo and are copies,
not imports. The two repos share no code at runtime.

## `sysfs-raptorlake/` and `sysfs-raptorlake-noroot/` — generated

Built by `python tools/make_fixture_sysfs.py`. Do not edit them by hand.

These predate the real capture. **Two devices carry real bytes** —
`0000:01:00.0` and `0000:02:00.0` are the `dumps/` captures, byte for byte —
and **every other device is synthesized**: its identity fields are taken from
the real machine's `lspci -tv` listing and the rest of its registers are
plausible values written by the generator.

The synthesized images are correct in *shape* — a Type 1 header really does
carry its bus numbers at `0x18`–`0x1A` — but they are not a recording of
hardware. A test that asserts something about a synthesized device is testing
this tool's decoding, not making a claim about real silicon. Prefer the capture
trees for anything that is meant to be a statement about hardware.

## Why the directory names use dashes

A real sysfs entry is named `0000:01:00.0`. A colon is an ordinary character in
a Linux filename and is forbidden in one on Windows, so a fixture tree named
that way could not be checked out on both. The fixtures use `0000-01-00.0` and
write the true address into each device's `uevent` file as
`PCI_SLOT_NAME=0000:01:00.0` — which is where the kernel publishes it on a real
machine too. `sysfs.py` reads the directory name first and falls back to
`uevent`, so the same code reads both a fixture and a live `/sys`.

One consequence worth knowing: a captured tree has no symlinks, so it carries
none of the kernel's own parent/child nesting. `pcitopo tree` says so rather
than reporting a cross-check it did not perform — the nesting comparison only
runs against a live `/sys`.
