# dls-plc-tools

Tools for interrogating Omron NX PLCs over EtherNet/IP, supporting EPICS
commissioning at Diamond Light Source. Two front-ends over the `aphyt`
library, exposed as subcommands of the single `dls-plc-tools` entry point (a
Typer app in `__main__.py`): `interrogate.py` (CLI, `dls-plc-tools read`, core
`read_plc()`) and `gui.py` (PySide6, `dls-plc-tools gui`, core `run_gui()`). The
entry point matches the package name so it runs with `uvx dls-plc-tools …`; the
subcommands lazy-import their heavy deps (aphyt / PySide6) so `--version`/`--help`
stay fast. `portforward.py` backs the GUI's optional k8s port-forward path (below).

## Port forwarding (GUI only)

PLCs on a private per-CIA network (`192.168.<cell>.0/24`) are reachable only from
the cluster's worker nodes. With the GUI's **Port forward** box ticked,
`portforward.py` stands up a throwaway socat Pod + LoadBalancer Service on the
(always acastus) cluster — the pod-pfwd shell helper moved into the tool — mapping
`service_ip:port` → `plc_ip:port` pinned to the given node, then aphyt connects to
the resolved Service IP. The **Service IP** field accepts a literal IP *or* a DNS
name (e.g. `sr19c-pfwd`); `resolve_host` (`socket.gethostbyname`) resolves it to a
literal IP up front, because `loadBalancerIP`, the MetalLB sharing key, and
aphyt's connect all need the literal IP — a name would be rejected by k8s.
Node is used verbatim (its full name, e.g.
`sr19c-k8s-serv-01.pri.diamond.ac.uk`). In the GUI both **Node** and **Service
IP** are editable combos prefilled `sr01c`…`sr24c`; every top-bar field, plus the
Port-forward checkbox, can be prefilled from the CLI (`dls-plc-tools gui <ip>
-f/--port-forward --node … --service-ip … --port … --cluster …`). Ticking the
Port-forward box fires `request_nodes`, whose worker slot runs
`portforward.list_nodes()` (`kubectl get nodes -o name`, `node/` prefix stripped)
and repopulates the Node combo with the cluster's *live* node names — so the list
tracks the cluster with no code change, and a picked name is a guaranteed-valid
`nodeName` (avoiding the short-name-vs-FQDN mistake that strands a pod). It needs
a login, so it's fetched lazily on tick, not at startup; on failure the static
`sr01c`…`sr24c` fallback stays and the error shows in the status bar. The combo
stays editable, and a typed/prefilled `--node` is preserved across the refresh
(`setCurrentText`). `--cluster` (default `acastus`) selects which klogin to source
(`{KLOGIN_DIR}/{cluster}`) and is threaded through `PortForward`/`_kubectl`/
`check_auth`/`list_nodes`; only acastus actually has the shared-IP forward model,
so the arg makes the plumbing generic but not the feature. The **cluster is shown
in the GUI** in the Port-forward checkbox label (`Port forward (acastus)`) and the
connect status line. A **pod-status LED** (`Pod:` in the top bar) is driven by the
worker's `forward_status(state, name)` signal — `pending` (orange) emitted before
`up()`, `good` (green) after the session opens, `inactive` (red) from
`disconnect_plc` (which the failure path also routes through, so a failed
stand-up goes red). It shows the pod/service name (`name_for(port)` =
`pfwd-<user>-<port>`) while active so the pod is traceable, cleared on inactive.
The forward nodes are tainted
`location=<cell>` (observed `NoSchedule`, which `nodeName` already bypasses, so
the pod runs regardless); the pod still tolerates any `location` taint
(`operator: Exists`) as cheap insurance and to match the permanent forward's
pods, in case a node is ever `NoExecute`-tainted. Port defaults to the EtherIP
port 44818, pushed onto `connected_cip_dispatcher.explicit_message_port` when
overridden (aphyt's `connect_explicit` takes no port arg). The forward is created
on Connect and torn down on Disconnect and window close. `closeEvent` tears down
**synchronously** (`QMetaObject.invokeMethod(..., BlockingQueuedConnection)`): a
queued teardown loses the race with thread shutdown — the `kubectl delete`s
outlast the `wait()` (they sit through the pod's termination grace) and the
process exits mid-delete, leaking the pod/service.

Before creating anything, `up()` also runs `_check_free`: it lists Services in
the namespace and refuses to start if a *different* one already claims
`service_ip:port` (someone else forwarding the same node/PLC), so two users don't
collide or clobber each other. It skips our own same-named stale forward (which
`up()` replaces) and filters on the port, so the permanent per-cell forward that
legitimately shares the IP on the terminal-server ports (8026-8028) isn't flagged.
It checks declared Service ownership rather than TCP-probing, to avoid racing
MetalLB/kube-proxy routing lag. `list_nodes` likewise runs `check_auth` first so a
logged-out node refresh gives the friendly "log in" message, not a raw error.

`up()` waits for the socat pod to be Ready *and then* for `service_ip:port` to
actually accept a TCP connection (`_wait_reachable`) before returning. The pod
goes Ready a beat before MetalLB has allocated + L2-announced the shared IP and
kube-proxy has programmed endpoints; without the reachability gate the GUI
connected into a not-yet-routable IP, aphyt hit its connect timeout, and the
`except` in `connect_plc_forward` tore the whole forward straight back down —
which looked like "the pod never comes up" (it did; it was deleted milliseconds
later). Because failure tears the forward down, debug by pointing the CLI at a
hand-applied forward (`dls-plc-tools read <service_ip>`) rather than through the GUI.

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

Auth login is deliberately *not* performed by the tool: `_kubectl` runs captured
+ non-interactive and reuses whatever acastus credentials the user's shell
already has (like pod-pfwd). One sharp edge that cost a long debugging session:
klogin prints a **welcome banner to stdout** whenever it (re)authenticates, so
`_kubectl` redirects klogin's own stdout to `/dev/null` (`source "$KLOGIN"
>/dev/null && exec kubectl …`) — otherwise the banner contaminates captured
kubectl output and breaks exact parses like `_wait_ready`'s `"True"` check,
*intermittently* (only when the banner appears, i.e. when creds aren't already
warm).

The klogin kubeconfig authenticates through a **kubelogin exec plugin**
(`oidc-login get-token`, `--grant-type=password`, `interactiveMode: IfAvailable`,
token cache under `~/.kube/cache/acastus/oidc-login`). When the cached token has
expired the plugin can only refresh by prompting for a fedid/password on a TTY.
The tool never prompts, so two guards handle the logged-out case cleanly instead
of stalling:

- `_kubectl` forces stdin non-interactive (`DEVNULL` when nothing is piped in),
  so an expired token makes the plugin fail *fast* rather than block on a prompt
  until the timeout; and it wraps `subprocess.TimeoutExpired` as a
  `PortForwardError` so any residual stall is still a clean message, not a raw
  traceback.
- `up()` runs a `check_auth()` preflight (`kubectl auth whoami`, exit 0 iff
  authenticated) **before** creating anything, and on failure raises a
  `PortForwardError` telling the operator to log into acastus in a terminal
  (running `klogout` first if they see auth errors) and retry.

So the operator must still be logged into acastus in a terminal before
connecting; the change is that a stale/absent login now surfaces as an
actionable status-bar message up front instead of a silent hang. To exercise the
logged-out path on a real workstation, run **`klogout`** (on `PATH` after sourcing
klogin) and reconnect.

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
uv run dls-plc-tools read <ip>     # discover + summarise all structures
uv run dls-plc-tools read --help   # -l list, -s show members, -d live watch
uv run dls-plc-tools gui <ip>
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
