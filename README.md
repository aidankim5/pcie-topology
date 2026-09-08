# pcie-topology

`lspci -tv`, but every node is annotated and the tool shows its work.

It reads `/sys/bus/pci/devices/` on a Linux machine, decodes the configuration
space itself, rebuilds the PCI Express hierarchy from the bridge registers, and
then checks that answer against the one the kernel already published. Pure
standard-library Python 3, no third-party packages, no network access.

```
Domain 0000  (root bus 00)
├─ [0000:00:01.0]  Root Port  [01]  Intel Raptor Lake PCI Express 5.0 Graphics Port
│                  PCI bridge [060400]
│                  2.5 GT/s x16 (max 32 GT/s x16)  DEGRADED: speed 2.5 GT/s of 32 GT/s
│  ├─ [0000:01:00.0]  Legacy Endpoint  NVIDIA GA104 [GeForce RTX 3060 Ti]
│  │                  VGA compatible controller [030000]
│  │                  2.5 GT/s x16 (max 16 GT/s x16)  DEGRADED: speed 2.5 GT/s of 16 GT/s
│  └─ [0000:01:00.1]  Endpoint  NVIDIA GA104 High Definition Audio Controller
│                     Audio device [040300]
│                     2.5 GT/s x16 (max 16 GT/s x16)  DEGRADED: speed 2.5 GT/s of 16 GT/s
├─ [0000:00:06.0]  Root Port  [02]  Intel Raptor Lake PCI Express 4.0 Graphics Port
│                  PCI bridge [060400]
│                  16 GT/s x4 (max 16 GT/s x4)
│  └─ [0000:02:00.0]  Endpoint  Samsung NVMe SSD Controller S4LV008[Pascal]
│                     Non-Volatile memory controller [010802]
│                     16 GT/s x4 (max 16 GT/s x4)
```

## Install and run

There is nothing to install. Clone it and run the module.

```bash
git clone https://github.com/aidankim5/pcie-topology
cd pcie-topology

python3 -m pcitopo list      # every function, flat, with identity fields
python3 -m pcitopo buses     # every bridge's bus numbers, with consistency checks
python3 -m pcitopo tree      # the topology, annotated

sudo python3 -m pcitopo tree # the same, plus link speed and width
```

Off the hardware, against the checked-in captures:

```bash
python3 -m pcitopo tree --sysfs-root tests/fixtures/sysfs-capture-raptorlake
python3 -m pcitopo tree --sysfs-root tests/fixtures/sysfs-capture-raptorlake-noroot
```

### Options

| Flag | What it does |
| --- | --- |
| `--sysfs-root PATH` | read a captured tree instead of the live `/sys` |
| `--ids PATH` | use a specific `pci.ids` file for vendor and device names |
| `-v`, `--verbose` | add BARs, bridge forwarding windows and the capability chain |
| `--degraded` | list only links running below their maximum |
| `--json` | emit the topology as JSON |
| `--dot FILE` | write a Graphviz block diagram (`-` for stdout) |

```bash
python3 -m pcitopo tree --dot topo.dot
dot -Tsvg topo.dot -o topo.svg
```

Exit codes: `0` done, `1` sysfs missing or unreadable, `2` usage error, `3` not
built yet.

## How the tree gets built

The rule is one sentence:

> **A device on bus N is a child of whichever bridge has Secondary Bus Number == N.**

Everything else is consequence. Here is where those numbers come from.

**1. Header Type at offset `0x0E` splits every function in two.** Bits 6:0 are
the layout: `0` is a Type 0 endpoint, `1` is a Type 1 bridge. Bit 7 is the
multifunction flag, which says nothing about the layout — masking it off before
looking at the layout is the first thing `header.py` does.

**2. A Type 1 header spends four of its BARs on bus numbers instead.** Where an
endpoint keeps BAR2 through BAR5, a bridge keeps three single bytes:

| Offset | Register | Meaning |
| --- | --- | --- |
| `0x18` | Primary Bus Number | the bus this bridge sits **on** |
| `0x19` | Secondary Bus Number | the bus immediately **below** it |
| `0x1A` | Subordinate Bus Number | the **highest** bus number below it |

One byte each, so there is no little-endian flip here — the only place in the
whole tool where a multi-byte read would be wrong. A bus number is 8 bits
(the routing ID is 8 bits of bus, 5 of device, 3 of function), so the register
is exactly one byte wide and the question of byte order never arises.

**3. Those numbers are a routing rule, not labels.** A configuration
transaction carries the bus number it is aimed at, and every bridge compares it
against its own two boundaries:

```
target == secondary                 claim it, talk to a device on that bus
secondary < target <= subordinate   forward it further downstream
anything else                       not mine, ignore it
```

So `[secondary, subordinate]` is a *range of buses this bridge is responsible
for*, the address space is partitioned by those ranges, and exactly one bridge
can answer for any given bus. That uniqueness is what makes step 4 a lookup
rather than a search.

**4. Match every device's own bus against those secondaries.** Devices on the
root bus have no bridge above them — nothing forwards to bus 0, the root
complex presents it directly — so they hang off the domain. The Host Bridge at
`00:00.0` sits *on* bus 0 as a peer of everything else there; it is not their
parent, and treating it as one would nest the entire machine under it.

**5. Then check the answer.** sysfs already knows: each entry in
`/sys/bus/pci/devices` is a symlink into `/sys/devices/`, nested the way the
kernel believes the hardware is.

```
0000:01:00.0 -> ../../../devices/pci0000:00/0000:00:01.0/0000:01:00.0
```

The kernel built that from the same registers, but through its own code, at
boot. It is a genuinely independent answer, so the tool compares the two and
**reports a disagreement instead of picking a winner** — if they differ, one of
them is wrong and the tool cannot know which.

`pcitopo buses` runs the invariants the tree depends on, before the tree is
drawn: no range runs backwards, no two bridges claim the same bus, every
populated bus above the root is some bridge's secondary, and no child range
escapes its parent. A tree drawn from bad bus numbers looks exactly as
convincing as a good one, which is why the numbers get their own verb.

## The privilege boundary

This is the single most important thing to understand about running the tool,
and it is a line at one offset.

```
offset  0x00 ─────────────────── 0x40 ──────────────── 0x100 ─────── 0xFFF
        │  the header (64 B)     │  capability chain   │  extended    │
        │  vendor, device, class │  ← link speed and   │  caps        │
        │  revision, Header Type │    width live here  │              │
        │  bridge bus numbers    │                     │              │
        └────────────────────────┴─────────────────────┴──────────────┘
         ↑ any user                ↑ root only
```

Reading `config` as a normal user returns **exactly 64 bytes**. That is
deliberate kernel policy, not an error and not a short file: `pci_read_config()`
in `drivers/pci/pci-sysfs.c` starts at `size = 64` and raises it to the device's
full `cfg_size` only for a reader holding `CAP_SYS_ADMIN`.

What lands inside those 64 bytes: identity, Header Type at `0x0E`, and a
bridge's bus numbers at `0x18`–`0x1A`. **So the entire tree is buildable with no
privileges at all** — `list`, `buses` and `tree` all work as a normal user.

What falls outside: the capability chain, which starts at `0x40` at the
earliest, and the PCI Express Capability in it that carries link speed and
width. Those need `sudo`, and the tool says so rather than quietly printing
less.

## Reading a link annotation

Every PCIe link has two descriptions, and the interesting ones disagree:

- **Link Capabilities** (`cap + 0x0C`) — what the port is built for
- **Link Status** (`cap + 0x12`) — what it actually trained to

`--degraded` lists every device where current is below maximum. That is a
discrepancy, not automatically a fault, and the tool says so:

- an idle GPU parks itself at 2.5 GT/s and comes back up on demand — that is
  power management working, and it is why the sample output above shows a
  Gen4 card at Gen1 rates;
- a root port faster than the card plugged into it will always read as
  degraded, because the link negotiates to the slower of the two ends;
- an empty slot reports width 0, which means no link trained at all, and is
  excluded rather than reported as degraded.

What is worth chasing is a device that should be busy and is not at full rate.

## Concepts, tagged

The repo is a learning project, so each concept is marked with whether it was
already understood going in.

**[taught] — known before this tool was written**

- BDF addressing (`domain:bus:device.function`)
- the configuration space layout and its offsets
- little-endian multi-byte fields
- walking the capability chain from `0x34`
- decoding Link Status
- the three address spaces, and BARs
- Type 0 versus Type 1 headers, and the Header Type fork at `0x0E`

**[ahead] — new here**

- Secondary and Subordinate Bus Numbers as a *routing rule*, and the
  realisation that they describe a relationship rather than a device
- how a tree falls out of comparing one register against one address field
- Device/Port Type (`cap + 0x02`, bits 7:4): root port, endpoint, switch port
- Link Capabilities versus Link Status as a pair, and what their gap means
- bridge forwarding windows at `0x20`/`0x24`, and 1 MB granularity — why a
  16-bit register can hold a 32-bit address
- 64-bit BARs consuming the register after them
- extended capabilities at `0x100` and the packed 32-bit header
- PCI domains, and why every bus lookup has to be keyed on `(domain, bus)`

Every module docstring cites the spec section it implements and marks anything
that is the tool's decision rather than the standard's as *"choice, not spec"*.

## Module map

| File | Job |
| --- | --- |
| `config_space.py` | bytes plus the little-endian readers, and `covers()` so callers degrade instead of raising |
| `sysfs.py` | the filesystem read. Pure I/O, no interpretation |
| `ids.py` | `pci.ids` parsing, with a built-in fallback table and then raw hex. No network, ever |
| `header.py` | the common header and the Header Type fork at `0x0E` |
| `bridges.py` | the Type 1 bus numbers, and what they claim |
| `bars.py` | BARs, and the bridge forwarding windows |
| `capabilities.py` | the chain from `0x34` and `0x100`, and the PCI Express Capability |
| `model.py` | merges decoded bytes with the kernel's attribute files, records disagreements |
| `topology.py` | builds the tree, then cross-checks it against sysfs nesting |
| `render.py` | text output. Nothing here decodes |
| `export.py` | JSON and DOT, as renderers over the same objects |
| `cli.py` | argparse |

## Tests

```bash
python3 -m unittest discover
```

161 tests, no hardware and no third-party packages required.

The suite is anchored to something independent wherever it can be. Identity is
checked field by field against `lspci -nn`, and the bridge bus numbers against
`lspci -tv`, both captured in the same boot as the configuration-space bytes.
The two fixture trees are that one capture at two privilege levels, which lets
the tests *prove* the claim above rather than assert it: the tree built from
64-byte reads is identical to the tree built from full ones, and the link
annotations are the only thing that disappears.

See `tests/fixtures/README.md` for what is real in the fixtures and what is
synthesized.

## Hardware constraint

Developed on a Windows machine with no WSL, so the live `/sys` is only
reachable by booting a Ubuntu live USB. `tools/import_capture.py` turns one such
boot into a checked-in fixture tree; `capture.sh` in the project notes is what
runs on the Linux side. Ubuntu's live image already ships `python3` and
`pciutils`, so there is nothing to install there either.

---

Built with [Claude Code](https://claude.com/claude-code) as a learning project:
the design decisions, the spec citations and the code are worked through in
conversation rather than generated wholesale, and the `[taught]`/`[ahead]` tags
above track what that actually taught.
