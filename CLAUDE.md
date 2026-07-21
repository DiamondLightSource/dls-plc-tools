# dls-plc-tools

Tools for interrogating Omron NX PLCs over EtherNet/IP, supporting EPICS
commissioning at Diamond Light Source. Two front-ends over the `aphyt`
library: `interrogate.py` (CLI, entry point `dls-read-plc`) and
`gui.py` (PySide6, entry point `dls-read-plc-gui`). `portforward.py` backs the
GUI's optional k8s port-forward path (below).

## Port forwarding (GUI only)

PLCs on a private per-CIA network (`192.168.<cell>.0/24`) are reachable only from
the cluster's worker nodes. With the GUI's **Port forward** box ticked,
`portforward.py` stands up a throwaway socat Pod + LoadBalancer Service on the
(always acastus) cluster — the pod-pfwd shell helper moved into the tool — mapping
`service_ip:port` → `plc_ip:port` pinned to the given node, then aphyt connects to
the operator-supplied Service IP (a fixed-pool IP; `loadBalancerIP` must be a
literal IP, not a DNS name). Node is used verbatim (its full name, e.g.
`sr19c-k8s-serv-01.pri.diamond.ac.uk`). The forward nodes are tainted
`location=<cell>` (observed `NoSchedule`, which `nodeName` already bypasses, so
the pod runs regardless); the pod still tolerates any `location` taint
(`operator: Exists`) as cheap insurance and to match the permanent forward's
pods, in case a node is ever `NoExecute`-tainted. Port defaults to the EtherIP
port 44818, pushed onto `connected_cip_dispatcher.explicit_message_port` when
overridden (aphyt's `connect_explicit` takes no port arg). The forward is created
on Connect and torn down on Disconnect and window close.

`up()` waits for the socat pod to be Ready *and then* for `service_ip:port` to
actually accept a TCP connection (`_wait_reachable`) before returning. The pod
goes Ready a beat before MetalLB has allocated + L2-announced the shared IP and
kube-proxy has programmed endpoints; without the reachability gate the GUI
connected into a not-yet-routable IP, aphyt hit its connect timeout, and the
`except` in `connect_plc_forward` tore the whole forward straight back down —
which looked like "the pod never comes up" (it did; it was deleted milliseconds
later). Because failure tears the forward down, debug by pointing the CLI at a
hand-applied forward (`dls-read-plc <service_ip>`) rather than through the GUI.

The Service IP is normally already owned by a *permanent* per-node forward
(e.g. `common-services/services/sr19c-pfwd`, whose Helm chart exposes terminal
servers on the same fixed IP). MetalLB won't let a second LoadBalancer claim an
owned IP unless **both** services carry the
`metallb.universe.tf/allow-shared-ip` annotation set to the same key **and**
their ports don't overlap. The tool annotates its Service keyed on the Service IP
itself; the permanent chart must set the matching key (its `values.yaml` now does,
via `service_annotations`). Our 44818 doesn't clash with the terminal-server ports.
Without this you get `address also in use … can't change sharing key`. It needs `kubectl` + the
acastus klogin, absent from CI/dev containers, so `portforward.py` is tested with
`subprocess.run` mocked and the whole path is verify-on-a-real-workstation. The
forward is transport only — the read-only rule below still holds.

Auth is deliberately *not* handled: `_kubectl` runs captured + non-interactive
and reuses whatever acastus credentials the user's shell already has (like
pod-pfwd). One sharp edge that cost a long debugging session: klogin prints a
**welcome banner to stdout** whenever it (re)authenticates, so `_kubectl`
redirects klogin's own stdout to `/dev/null` (`source "$KLOGIN" >/dev/null &&
exec kubectl …`) — otherwise the banner contaminates captured kubectl output and
breaks exact parses like `_wait_ready`'s `"True"` check, *intermittently* (only
when the banner appears, i.e. when creds aren't already warm). The operator must be logged into acastus in a terminal before
launching the GUI. Known rough edge (left as-is, to be characterised on a real
workstation): if acastus needs an interactive/browser login that isn't cached,
the first captured `kubectl` call could stall until its 30 s timeout rather than
prompt. If that turns out to bite, add a preflight auth check with a clear
"log into acastus first" message.

## Hard rule: never impact the PLC

These tools run against production vacuum PLCs. Everything must be
**read-only explicit messaging** (the same mechanism the EPICS ether_ip
driver uses continuously, serviced in the controller's spare time — not the
task scan). Specifically:

- Never call `write_variable` or anything that changes controller state.
- Never use aphyt's `start_keep_alive()` — it polls `list_services` every
  50 ms.
- Never let a timer queue up requests: one request in flight, skip ticks if
  the PLC is slow (the GUI's `_waiting` flag does this).
- Default poll rates ~2 s; floor at 0.5 s.

## aphyt gotchas (each of these has bitten us)

Verify signatures with `inspect` before using an aphyt method you haven't
already exercised — its sibling classes have inconsistent APIs.

- `omron.NSeries` is the **sync wrapper** used here:
  `connect_explicit(host, connection_timeout=None)` then
  `register_session()` — no `retry_time`/`max_attempts`/`connection_status`;
  those belong to `NSeriesThreadDispatcher`, a different class we don't use.
  A bad IP raises a socket error after the timeout; no retry loop.
- The reliable read primitive is a **whole-tag read**: `read_variable("Seq")`.
  Member-path reads (`read_variable("Seq.Desc")`) take a fragile code path
  (Omron rejects `get_attribute_all` on sub-paths; string-array members then
  read element-by-element and fail). Read the whole structure and slice
  client-side — it is also fewer requests.
- `read_variable` returns native Python values for scalars but
  `CIPStructure`/`CIPArray` instances for compound types; call `.value()` on
  a `CIPArray` to get a list.
- Unknown structure members raise `KeyError` (client-side resolution), not
  `CIPException` — catch both.
- The tag dictionary lives at `connected_cip_dispatcher.user_variables` /
  `.system_variables` / `.variables`; populated fully by
  `update_variable_dictionary()` (slow — walks every tag including the
  `_`-prefixed system ones) and incrementally by each `read_variable`.
  `variable_list()` is a method returning names only.
- `NSeries.__enter__` installs a SIGINT handler — fine in the CLI, but do
  not use the context manager off the main thread (the GUI worker constructs
  directly and calls `close_explicit()`).

## DLS PLC conventions

- Structures are **structure-of-arrays**: `Seq.Interfc[1]` is instance 1.
  Members are declared `ARRAY[0..N]`; index 0 is unused padding, so capacity
  is N instances. Declared capacity ≠ instances in use: a non-empty `Desc`
  entry marks a real instance (`USED_MARKER_MEMBER` in `interrogate.py`).
- Tags visible over EtherNet/IP = globals with **Network Publish** set in
  Sysmac Studio, plus the controller's `_` system variables. Enumeration
  uses Omron's Tag Name Server object (CIP class 0x6A) — something the EPICS
  ether_ip driver cannot do (it has no browse service; see its `CN_Services`
  enum).
- The reference spreadsheet for structure layouts is
  `NX PLC EPICS TAGS.xlsx` in the `dlsPLC` EPICS support module.
- `PLC_Name`, `PLC_Status`, `Epics_Num_dev` are conventionally published
  info tags (`INFO_TAGS`).

## Development

```bash
uv sync
uv run dls-read-plc <ip>     # discover + summarise all structures
uv run dls-read-plc --help   # -l list, -s show members, -d live watch
uv run dls-read-plc-gui <ip>
uv run tox -p                       # pre-commit, pyright, pytest
```

- Pre-commit runs ruff with bugbear etc.: `zip()` needs `strict=`, Qt method
  overrides need `# noqa: N802`.
- **PySide6 is pinned `<6.10`**: 6.10+ wheels are manylinux_2_34 and DLS
  RHEL 8 workstations have glibc 2.28. Remove the pin when workstations move
  to RHEL 9; expect Renovate to propose bumps that must be held back until
  then. GUI deps are core dependencies, not an extra — uv extras aren't
  sticky, so a plain `uv sync` would silently uninstall them.
- No PLC is reachable from CI or dev containers. Test logic with mocked
  `read_variable` payloads; test the GUI headless with
  `QT_QPA_PLATFORM=offscreen` (grab-and-assert on the table works well).
  Anything touching a new aphyt code path needs a check against a real PLC —
  say so in the PR if that hasn't been done.

## Releases

Pushing a git tag runs the full CI matrix and, on success, publishes to PyPI
via trusted publishing (GitHub environment `release`) and creates a GitHub
release. Any tag triggers this — don't push scratch tags. Version comes from
the tag via setuptools_scm.
