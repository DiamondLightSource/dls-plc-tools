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
