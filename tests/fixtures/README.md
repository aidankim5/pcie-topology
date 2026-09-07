# Fixtures: what is real and what is not

Tests must run with no PCI hardware present, so they read a directory that has
the same shape as `/sys` and the same bytes in it. Nothing in `pcitopo/sysfs.py`
does anything cleverer than listing a directory and opening the files in it, so
a fixture tree exercises the real code path.

## `dumps/` — captured from hardware

| File | What it is |
| --- | --- |
| `rtx3060ti_01-00.0.txt` | `sudo lspci -vvv -xxxx -s 01:00.0`, 4096 bytes of real configuration space from an NVIDIA RTX 3060 Ti |
| `samsung990pro_02-00.0.txt` | the same, for a Samsung 990 PRO NVMe SSD |
| `lspci-tv-raptorlake.txt` | `lspci -tv` on the same machine: the topology, as the system's own tool sees it |

These came from the `pcie-config-decoder` repo and are copies, not imports. The
two repos share no code at runtime.

`lspci-tv-raptorlake.txt` is the answer key. The tree this tool builds from
bridge registers has to match the tree `lspci` built independently, and the
tests check that.

## `sysfs-raptorlake/` and `sysfs-raptorlake-noroot/` — generated

Built by `python tools/make_fixture_sysfs.py`. Do not edit them by hand.

- `sysfs-raptorlake/` holds full configuration-space images.
- `sysfs-raptorlake-noroot/` is the identical machine with every `config` file
  truncated to 64 bytes, which is exactly what a non-root read returns. It
  exists so the degraded path is tested, not assumed.

**Two devices carry real bytes.** `0000:01:00.0` and `0000:02:00.0` are the
captures above, byte for byte.

**Every other device is synthesized.** Its identity fields (vendor, device,
class, bus numbers) are taken from the real machine's `lspci -tv` listing, and
the rest of its registers are plausible values written by the generator. The
synthesized images are correct in *shape* — a Type 1 header really does carry
its bus numbers at 18h–1Ah — but they are not a recording of hardware. A test
that asserts something about a synthesized device is testing this tool's
decoding, not making a claim about real silicon.

## Why the directory names use dashes

A real sysfs entry is named `0000:01:00.0`. A colon is an ordinary character in
a Linux filename and is forbidden in one on Windows, so a fixture tree named
that way could not be checked out on both. The fixture uses `0000-01-00.0` and
writes the true address into each device's `uevent` file as
`PCI_SLOT_NAME=0000:01:00.0` — which is where the kernel publishes it on a real
machine too. `sysfs.py` reads the directory name first and falls back to
`uevent`, so the same code reads both a fixture and a live `/sys`.
