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
  a `CIPArray` to get a list. `.value()` is *not* uniformly a Python value:
  BYTE/WORD/DWORD/LWORD/TIME return raw `bytes`, an array of structs returns a
  list of `CIPStructure`, and `CIPStructure.value()` returns the struct itself
  (it mutates members in place). Go through `structure.py` rather than
  `str()`-ing whatever comes back — see the nested-structures section.
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

## Nested structures (members that aren't CIP primitives)

Most members are primitives, so a member is a column. Some are not: a member can
be an `ARRAY[..] OF <struct>` (`V.ILK[x]`, "Array [1..xxx] of EPICS Valve INTLK
STRUCT"), a plain nested struct, or a struct inside one of those
(`Gauge[x].IMG.W`, alongside a 16-bit `IMG.B`). aphyt decodes all of them, but
`CIPArray.value()` then hands back a list of `CIPStructure` objects, and
`str()` of one is aphyt's repr — which is what used to land in a table cell:

    Ilk[1] ->  { type: ILK_Type | members: {'Trip': 1, 'Msg': HiVac} }

`structure.py` is the single answer for all three front-ends. `flatten()` turns
a read result into a tree of `Member` nodes keyed by dotted path; leaves hold
one value per instance row, branches hold children *plus* a per-row summary
(`Trip=1 Msg=HiVac`) so they can be shown collapsed. `columns_for(members,
expanded)` picks the visible columns. Rules that matter:

- Rows stay one-per-instance. An `ARRAY OF <struct>` has exactly one struct per
  row, so its leaves line up with the flat members — this is why flattening
  works at all, and why it must not be applied to top-level arrays.
- **A plain nested struct is another structure-of-arrays, not one value.**
  `PSU.Status.W[1]` is the convention applied one level down: `Status` groups
  per-instance members, so `Status.W` is a column with one value per row. It is
  *not* a scalar living on row 0 — treating it that way strands every
  instance's value in one comma-joined cell (`16#0000,16#002C,16#0000,…`).
  `_member()` therefore recurses into it with the same rules as the tag root.
  Its bytes are only sliced into its members when `value()` is called on it —
  an array's own `value()` already does that per element, a plain member's does
  not.
- **The parent decides whether a nested array is instance-indexed.** Under a
  plain nested struct it is (above). Under an `ARRAY OF <struct>` it is not:
  `Gauge[x].IMG.B` is one gauge's own 16 bits, so it stays joined in a single
  cell rather than adding 16 columns. All-boolean lists render as a bit string
  in groups of eight, index 0 leftmost. `_member()` handles the first context,
  `_child()` the second — keep them distinct.
- **BYTE/WORD/DWORD/LWORD return raw `bytes` from aphyt's `value()`**, so every
  DLS status word (`ILK_Status`, `Control`, `Fault`, `PSU.Status.W`) renders as
  `b'A\x00'` unless formatted. `cell_text` writes them `16#0041`, the notation
  the tag spreadsheet uses.
- Type names are mapped to PLC names (`INT`, `WORD`, `ARRAY[0..15] OF BOOL`,
  and a struct's own `variable_type_name`), not `CIPInteger`.

GUI: branches start collapsed as one summary column headed `▸ Ilk`; clicking the
header expands it into `Ilk.Trip`/`Ilk.Msg` and the header becomes `▾ Ilk`. The
branch column is kept when expanded because it *is* the collapse affordance.
The last read is cached (`_members`), so expanding, filtering and the in-use
toggle redraw without another PLC read, and expansion survives a poll but is
dropped when a different tag is watched. The filter matches descendant paths, so
filtering `Trip` still surfaces a collapsed `Ilk`. CLI: `-m` takes dotted paths
and expands just enough to reach them (`-m Desc,IMG.W`); naming a branch leaves
it collapsed; `-e/--expand-all` shows every leaf and drops the now-redundant
branch columns. `-s/--show` prints the tree, and the paths it prints are exactly
what `-m` accepts. The Excel export always expands fully — a sheet has no
expand affordance — one row per dotted leaf path, native scalars left numeric.

Unverified against hardware: aphyt assigns nested struct members with
`members[name] = ...` rather than `add_member`, so a nested struct's
`_alignment` stays 0 and its parent's `value()` applies no alignment padding
beyond a 1-byte bump. If a nested struct's members force an alignment >2 the
offsets could be wrong and the *values* (not just their display) would be
garbage. Check a known nested tag against Sysmac before trusting deep nesting.

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

### Never let `uv` touch `.venv` — always set `UV_PROJECT_ENVIRONMENT`

**Export `UV_PROJECT_ENVIRONMENT=/root/.venvs/<repo>` before any `uv` command in
`/workspaces`.** Not a fallback — the default, every time.

`/workspaces` is a bind mount shared between the host user (`jjc62351`) and
containers running as `root`, but a `.venv` records its interpreter's
**absolute path**. The owner's venvs point at
`/home/jjc62351/.local/share/uv/python/...` or `/usr/bin/python3.11` — correct
on the host, and *expected* to look dangling from inside a container. That is
the healthy state, not a fault to repair.

The damage runs the other way. uv reacts to an interpreter it cannot resolve by
**deleting the venv and rebuilding it** — one `warning:` line that scrolls past,
**exit 0**, looks like a slow first run. The rebuild repoints `.venv` at
`/root/.local/share/uv/python/...`, so the repo is handed back **broken on the
host**, and the owner's next `uv run` rebuilds it back again. Ping-pong, once
per session, plus anything outside `uv.lock` (a hand-installed `pyinstaller`
for a local exe build, an editable sibling module) destroyed each way.

Checking `.venv/bin/python -V` first does *not* protect against this: after uv
has rebuilt the venv the probe passes happily, and it is the host copy that is
now broken. Only keeping the environment off the shared mount does. Measured on
uv 0.9.9 — no flag helps:

| command | `.venv` uv cannot resolve |
| --- | --- |
| `uv run …`, `uv run --no-sync …` | **rebuilt, repointed at the container** |
| `uv sync`, `uv sync --frozen` | **rebuilt, repointed at the container** |
| `uv pip list` | untouched (not a project command) |
| anything with `UV_PROJECT_ENVIRONMENT` set off-mount | untouched ✓ |

If a repo's `.venv` is already pointing into `/root`, a container run has
clobbered it. It self-heals — the owner's next `uv` command on the host rebuilds
it — but say so rather than leaving them to hit it.

```bash
export UV_PROJECT_ENVIRONMENT=/root/.venvs/dls-plc-tools   # do this first
uv sync
uv run dls-plc-tools read <ip>     # discover + summarise all structures
uv run dls-plc-tools read --help   # -l list, -s show members, -d live watch
uv run dls-plc-tools read <ip> -d V -e   # live watch, nested members expanded
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
  `tests/nested.py` builds the CIP objects aphyt would return for the nested
  shapes the tag spreadsheet declares (array-of-struct, nested struct, nested
  BOOL array), byte layouts included — reuse it rather than hand-rolling more.
- **`tests/test_gui.py` skips itself where `libGL.so.1` is missing** (this dev
  container), so a GUI regression can sit green locally and only fail in CI —
  three `QComboBox.setText` calls had rotted that way. If you touch the GUI,
  actually run those tests: fetch the system libs into a sysroot
  (`apt-get download --print-uris libgl1 libglx0 libglvnd0 libfontconfig1
  libfreetype6 libx11-6 libxcb1 libxau6 libxdmcp6 libbsd0 libmd0 libpng16-16t64
  libbrotlidec1 libbrotlicommon1 libexpat1 libxkbcommon0 libdbus-1-3` → curl →
  `dpkg-deb -x`) and point `LD_LIBRARY_PATH` at it. apt's own download method
  fails here (it cannot `setgroups`), hence `--print-uris` + curl.
  Anything touching a new aphyt code path needs a check against a real PLC —
  say so in the PR if that hasn't been done.

## Releases

Pushing a git tag runs the full CI matrix and, on success, publishes to PyPI
via trusted publishing (GitHub environment `release`) and creates a GitHub
release. Any tag triggers this — don't push scratch tags. Version comes from
the tag via setuptools_scm.
