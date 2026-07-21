[![CI](https://github.com/DiamondLightSource/dls-plc-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/DiamondLightSource/dls-plc-tools/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/DiamondLightSource/dls-plc-tools/branch/main/graph/badge.svg)](https://codecov.io/gh/DiamondLightSource/dls-plc-tools)
[![PyPI](https://img.shields.io/pypi/v/dls-plc-tools.svg)](https://pypi.org/project/dls-plc-tools)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# dls_plc_tools

Tools for interrogating Omron NX PLCs over EtherNet/IP, to support EPICS
commissioning of PLC-based vacuum systems at Diamond Light Source.

They let you browse the tags a PLC publishes, inspect the layout of its
structures, and watch values update live — without a Sysmac Studio licence or
physical access to the controller. Everything is **read-only explicit
messaging**: the same mechanism the EPICS `ether_ip` driver uses, serviced in
the controller's spare time, so it is safe to run against production PLCs.

There are two front-ends over the [`aphyt`](https://pypi.org/project/aphyt/)
library: a command-line tool (`dls-read-plc`) and a PySide6 desktop GUI
(`dls-read-plc-gui`).

What            | Where
:---:           | :---:
Source          | <https://github.com/DiamondLightSource/dls-plc-tools>
PyPI            | `pip install dls-plc-tools`
Docker          | `docker run ghcr.io/diamondlightsource/dls-plc-tools:latest`
Releases        | <https://github.com/DiamondLightSource/dls-plc-tools/releases>

## Command-line tool

With just an IP address, `dls-read-plc` discovers every published
structure tag on the PLC and summarises each one — its member layout and how
many instances are actually in use:

```
dls-read-plc 172.23.x.x
```

Other modes:

```
dls-read-plc 172.23.x.x -l              # list every published tag and its type
dls-read-plc 172.23.x.x -s Seq          # show one structure's members and array bounds
dls-read-plc 172.23.x.x -d Seq          # live-updating table of a structure's values
dls-read-plc 172.23.x.x -d Seq -m Desc,State,Status   # choose which members to watch
```

`--detail`/`-d` redraws a table every 2 seconds (floor 0.5 s, set with
`--interval`), highlighting cells that have changed. Press Ctrl-C to stop. See
`dls-read-plc --help` for the full option list.

## GUI

```
dls-read-plc-gui 172.23.x.x
```

`dls-read-plc-gui` gives you a browsable list of the PLC's published tags, a live
table of a selected structure's values (changed cells flash amber), and an
export of the discovered structure layouts to an Excel spreadsheet. The IP
address argument is optional — you can also enter it in the window.

### Reaching a PLC on a private network (port forwarding)

Some PLCs sit on a private per-CIA network (`192.168.<cell>.0/24`) reachable only
from the cluster's worker nodes, not from a normal workstation. Tick **Port
forward** in the GUI to enable the extra fields and connect via a temporary
socat forward on the (acastus) cluster:

- **Node** — the worker node with access to that private network. The dropdown
  is prefilled with `sr01c`…`sr24c` (`srNNc-k8s-serv-01.pri.diamond.ac.uk`); it
  is editable, so type over it for any other node.
- **Service IP** — a fixed-pool IP for the forward's LoadBalancer, or a DNS name
  that resolves to one (the dropdown offers `sr01c-pfwd`…`sr24c-pfwd`, also
  editable). A name is resolved to its IP before use; that resolved IP is the
  address the GUI then connects to.
- **Port** — the port forwarded on both sides, defaulting to the EtherIP port
  44818.

On **Connect** the tool stands up a throwaway socat pod + LoadBalancer service
(`service_ip:port` → `plc_ip:port`, pinned to the node) and connects to the
Service IP; **Disconnect** (and closing the window) tears both down. This path
needs `kubectl` and the acastus klogin, so it only works on a DLS workstation.
Leave **Port forward** unticked to connect directly to the IP, as before.

Every field can be prefilled from the command line, which also ticks the box —
handy for a specific cell. This opens the GUI ready to connect to the sr19c PLC
through its forward:

```
dls-read-plc-gui 192.168.19.10 --port-forward \
  --node sr19c-k8s-serv-01.pri.diamond.ac.uk --service-ip sr19c-pfwd
```

`--service-ip` takes an IP or a DNS name, `--port` overrides 44818, and `-f` is
short for `--port-forward`. See `dls-read-plc-gui --help` for all options.

The Service IP is normally already owned by a permanent per-node forward that
exposes other services on the same fixed IP (e.g. the `sr19c-pfwd` Helm release
under `common-services/`). Sharing one LoadBalancer IP between that service and
this tool's needs MetalLB's `metallb.universe.tf/allow-shared-ip` annotation set
to the same key on both — the chart and the tool both key it on the IP itself, so
they match automatically, as long as the permanent service carries the
annotation.

> **Log into acastus first.** The tool has no login of its own — it reuses your
> shell's cluster credentials. Authenticate in a terminal before launching the
> GUI (source the acastus klogin and run e.g.
> `kubectl get pods -n accelerator`), then start the GUI from the same account.
> If you are not authenticated the forward step just fails with the `kubectl`
> error in the status bar; and if acastus needs an interactive (browser) login
> that isn't cached yet, the first attempt may appear to hang until it times
> out — log in in your terminal and retry.

## Installation

```
pip install dls-plc-tools
```

Or, for development, using [uv](https://docs.astral.sh/uv/):

```
git clone https://github.com/DiamondLightSource/dls-plc-tools.git
cd dls-plc-tools
uv sync
uv run dls-read-plc <ip>
```

## Notes

- These tools only ever **read**. They never write to the controller, never
  start a keep-alive poll, and keep at most one request in flight. Poll rates
  default to ~2 s and are floored at 0.5 s.
- Tags are visible over EtherNet/IP only if they are globals with **Network
  Publish** set in Sysmac Studio, plus the controller's `_`-prefixed system
  variables. Enumeration uses Omron's Tag Name Server object (CIP class 0x6A).
- DLS structures are structure-of-arrays: `Seq.Interfc[1]` is instance 1, with
  index 0 reserved as padding. A non-empty `Desc` entry marks an instance that
  is actually in use.
