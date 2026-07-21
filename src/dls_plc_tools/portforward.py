"""Stand up an ad-hoc k8s socat port forward so the GUI can reach a PLC that
sits on a private per-CIA network behind the cluster's worker nodes.

This is the pod-pfwd shell helper moved into the tool: it applies a throwaway
socat Pod plus a LoadBalancer Service (on a fixed-pool ``service_ip``) that
forwards the EtherIP port to the PLC, then tears both down again. Every command
goes through ``kubectl`` after sourcing the acastus klogin, exactly as pod-pfwd
does. Nothing here talks to the PLC directly or changes controller state.

kubectl and the klogin script only exist on DLS workstations, so this path is
mocked in tests and never runs in CI.
"""

import getpass
import json
import socket
import subprocess
import time

# These mirror pod-pfwd.
NAMESPACE = "accelerator"
IMAGE = "alpine/socat:1.7.4.4-r0"
EIP_PORT = 44818  # EtherNet/IP explicit-messaging port (aphyt's default)
# Per-cluster klogin scripts live under this directory (e.g. .../klogin/1.0/acastus).
# The cluster name selects which one to source; acastus is the only cluster set up
# with the MetalLB shared-IP forward model this tool relies on, so it's the default.
KLOGIN_DIR = "/dls_sw/kubernetes/klogin/1.0"
DEFAULT_CLUSTER = "acastus"

# MetalLB lets several LoadBalancer services share one IP only if each carries
# this annotation set to the same key and their ports don't overlap. The
# permanent per-node forward (e.g. common-services' sr19c-pfwd) that already owns
# service_ip must set the SAME key. Both sides key on the shared IP itself, so
# they match without extra configuration.
SHARED_IP_ANNOTATION = "metallb.universe.tf/allow-shared-ip"

READY_TIMEOUT = 15.0  # seconds to wait for the socat pod to become Ready
# seconds to wait for service_ip:port to actually accept a connection. The pod
# goes Ready well before MetalLB has allocated + L2-announced the shared IP and
# kube-proxy has programmed the endpoints, so we gate on real reachability too.
REACHABLE_TIMEOUT = 30.0
POLL_INTERVAL = 0.5
PROBE_TIMEOUT = 2.0  # per-attempt TCP connect timeout for the reachability probe


class PortForwardError(RuntimeError):
    """Raised when a kubectl command fails or the pod never becomes ready."""


def resolve_host(value):
    """Resolve a Service-IP field (a literal IP *or* a DNS name) to a literal
    IPv4 address.

    ``loadBalancerIP`` and the MetalLB sharing key must both be literal IPs, and
    aphyt connects to the same address, so resolve once up front. A literal IP
    passes straight through ``gethostbyname``; a name (e.g. ``sr19c-pfwd``) is
    looked up via DNS.
    """
    value = value.strip()
    try:
        return socket.gethostbyname(value)
    except OSError as e:
        raise PortForwardError(f"could not resolve service IP {value!r}: {e}") from None


def name_for(port):
    """Resource name shared by the pod and service, lowercased for RFC1123.

    Ported from pod-pfwd's ``name_for`` so a stale forward from an earlier run
    is replaced rather than duplicated.
    """
    return f"pfwd-{getpass.getuser()}-{port}".lower()


def _kubectl(args, stdin=None, timeout=30, cluster=DEFAULT_CLUSTER):
    """Run one kubectl command against ``cluster`` and return its stdout.

    Sources that cluster's klogin script (which sets up cluster auth in the shell
    env) and execs kubectl with ``args`` passed as positional parameters, so no
    user input is interpolated into the shell command. ``cluster`` only selects
    the klogin path — it is a fixed set of on-disk script names, never user text
    in the shell.

    klogin prints a welcome banner to stdout whenever it (re)authenticates. We
    redirect *its* stdout to /dev/null so that banner can't contaminate the
    captured kubectl output — otherwise parsers like ``_wait_ready``'s exact
    ``"True"`` match break intermittently (only when the banner happens to
    appear). ``exec kubectl`` inherits the shell's original stdout, so kubectl's
    own output is unaffected; klogin's stderr is kept so auth errors still show.
    """
    klogin = f"{KLOGIN_DIR}/{cluster}"
    cmd = [
        "bash",
        "-c",
        f'source "{klogin}" >/dev/null && exec kubectl "$@"',
        "bash",
        *args,
    ]
    # klogin's kubeconfig authenticates via a kubelogin exec plugin with
    # grant-type=password and interactiveMode=IfAvailable: with an expired token
    # and a TTY on stdin it would block on a fedid/password prompt until `timeout`
    # fires. We never prompt (see check_auth), so force stdin non-interactive —
    # DEVNULL when there's nothing to pipe in — so the plugin fails fast instead
    # of hanging. Calls that pipe a manifest (input=stdin) are already a pipe, not
    # a TTY, so they're non-interactive too.
    run_kwargs = {"capture_output": True, "text": True, "timeout": timeout}
    if stdin is None:
        run_kwargs["stdin"] = subprocess.DEVNULL
    else:
        run_kwargs["input"] = stdin
    try:
        result = subprocess.run(cmd, **run_kwargs)
    except subprocess.TimeoutExpired as e:
        raise PortForwardError(
            f"kubectl {' '.join(args)} timed out after {timeout:g}s "
            "(is acastus reachable and are you logged in?)"
        ) from e
    if result.returncode != 0:
        raise PortForwardError(
            f"kubectl {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def list_nodes(cluster=DEFAULT_CLUSTER):
    """Return the cluster's node names, in the exact form a Pod's ``nodeName``
    accepts.

    ``kubectl get nodes -o name`` yields ``node/<name>`` lines; we strip the
    ``node/`` prefix. Because the names come straight from the API they are
    guaranteed-valid ``nodeName`` values — picking one avoids the short-name vs
    FQDN mistake that strands a pod on a non-existent node. Requires a login to
    ``cluster``, so callers fetch this lazily rather than at startup; the
    ``check_auth`` preflight turns a logged-out state into the same actionable
    "log in in a terminal" message the connect path gives, rather than a raw
    kubectl error.
    """
    check_auth(cluster=cluster)
    out = _kubectl(["get", "nodes", "-o", "name"], cluster=cluster)
    return [line.split("/", 1)[-1] for line in out.split()]


def check_auth(timeout=10, cluster=DEFAULT_CLUSTER):
    """Verify the shell's credentials for ``cluster`` are usable, raising
    ``PortForwardError`` with an actionable message if not.

    klogin's kubeconfig authenticates via kubelogin's OIDC *password* grant
    (an exec credential plugin). When the cached token has expired the plugin
    can only re-authenticate by prompting for a fedid/password on a TTY — which
    this tool deliberately never provides (``_kubectl`` runs non-interactive).
    So probe once, up front, with a cheap read-only ``kubectl auth whoami``: it
    exits 0 for any authenticated user and non-zero otherwise. On failure, tell
    the operator to log in in a terminal rather than standing up a forward whose
    every kubectl call would fail the same way.
    """
    try:
        _kubectl(["auth", "whoami"], timeout=timeout, cluster=cluster)
    except PortForwardError as e:
        raise PortForwardError(
            f"not logged into {cluster} (or the session has expired). In a terminal, "
            f"source the {cluster} klogin and log in — run `klogout` first if you see "
            f"authentication errors — then retry.\n  ({e})"
        ) from None


def _manifest(name, node, plc_ip, service_ip, port):
    """A Pod + LoadBalancer Service (as a v1/List) mirroring pod-pfwd's YAML."""
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": name}},
        "spec": {
            "nodeName": node,
            "hostNetwork": False,
            "restartPolicy": "Always",
            "terminationGracePeriodSeconds": 3,
            # The per-cell forward nodes are tainted location=<cell>. nodeName
            # bypasses the scheduler (so a NoSchedule taint wouldn't block us) but
            # a NoExecute taint would evict the pod after it starts. Tolerate any
            # location taint regardless of value/effect, matching the permanent
            # forward's pods, without needing to know the cell here.
            "tolerations": [{"key": "location", "operator": "Exists"}],
            "containers": [
                {
                    "name": "socat",
                    "image": IMAGE,
                    "imagePullPolicy": "Always",
                    "args": [
                        f"tcp-l:{port},fork,reuseaddr",
                        f"tcp:{plc_ip}:{port}",
                    ],
                    "resources": {
                        "limits": {"cpu": "100m", "memory": "100Mi"},
                        "requests": {"cpu": "10m", "memory": "10Mi"},
                    },
                    "securityContext": {"allowPrivilegeEscalation": False},
                }
            ],
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {"app": name},
            # Share service_ip with the permanent forward that owns it (keyed on
            # the IP); our port (default 44818) must not overlap its ports.
            "annotations": {SHARED_IP_ANNOTATION: service_ip},
        },
        "spec": {
            "type": "LoadBalancer",
            "loadBalancerIP": service_ip,
            "sessionAffinity": "None",
            "selector": {"app": name},
            "ports": [
                {
                    "name": f"{name}-{port}",
                    "port": port,
                    "targetPort": port,
                    "protocol": "TCP",
                }
            ],
        },
    }
    return {"apiVersion": "v1", "kind": "List", "items": [pod, service]}


class PortForward:
    """A single socat pod + service forward. Remembers its name so teardown is
    exact and idempotent."""

    def __init__(self, cluster=DEFAULT_CLUSTER):
        self._name = None
        self._cluster = cluster

    def up(self, plc_ip, node, service_ip, port=EIP_PORT, progress=None):
        """Create the forward ``service_ip:port`` -> ``plc_ip:port`` pinned to
        ``node`` and block until it is Ready *and* routable.

        Deletes any stale forward of the same name first, matching pod-pfwd.
        ``progress`` is an optional ``callable(str)`` invoked at each phase so a
        caller (the GUI) can show what the multi-second stand-up is doing.
        """

        def report(msg):
            if progress is not None:
                progress(msg)

        # Fail fast with a clear "log into <cluster>" message before creating
        # anything, rather than letting each kubectl call fail cryptically.
        report(f"checking {self._cluster} login...")
        check_auth(cluster=self._cluster)
        # Don't clobber a forward someone else already stood up on this IP:port.
        report(f"checking {service_ip}:{port} is free...")
        self._check_free(service_ip, port)
        name = name_for(port)
        # Record the name before applying so down() can clean up even if the
        # apply or the readiness/reachability waits below raise. _delete is
        # idempotent (--ignore-not-found), so this is safe if nothing got created.
        self._name = name
        report(f"creating forward pod {name} on {node}...")
        self._delete(name)  # replace a stale forward rather than clash with it
        manifest = _manifest(name, node, plc_ip, service_ip, port)
        _kubectl(
            ["apply", "-f", "-"], stdin=json.dumps(manifest), cluster=self._cluster
        )
        report("waiting for socat pod to be ready...")
        self._wait_ready(name)
        # The pod being Ready isn't enough: aphyt connects to service_ip:port, and
        # the MetalLB/kube-proxy path converges a beat later. Gate on a real TCP
        # connect so the caller doesn't connect into a not-yet-routable IP and
        # tear the whole forward down over a transient timeout.
        report(f"waiting for {service_ip}:{port} to become routable...")
        self._wait_reachable(service_ip, port)
        return name

    def _check_free(self, service_ip, port):
        """Refuse to start if another LoadBalancer service already claims
        ``service_ip:port``, so we don't collide with — or clobber — a forward
        someone else stood up (two people forwarding the same node/PLC).

        Only a *different* service counts: our own same-named stale forward is
        replaced by ``up()``, so it's skipped. Matching on the port as well as
        the IP means the permanent per-cell forward that legitimately shares this
        IP on other ports (the terminal servers on 8026-8028) isn't mistaken for
        a conflict. Scoped to our namespace, where the only thing that would bind
        this port on this IP is another run of this tool. Checking declared
        ownership (not a TCP probe) avoids racing MetalLB/kube-proxy routing lag.
        """
        mine = name_for(port)
        out = _kubectl(
            ["get", "svc", "-n", NAMESPACE, "-o", "json"], cluster=self._cluster
        )
        for svc in json.loads(out).get("items", []):
            spec = svc.get("spec", {})
            name = svc.get("metadata", {}).get("name")
            if name == mine or spec.get("loadBalancerIP") != service_ip:
                continue
            if any(p.get("port") == port for p in spec.get("ports", [])):
                raise PortForwardError(
                    f"{service_ip}:{port} is already forwarded by service {name!r} "
                    f"in the {NAMESPACE} namespace — someone else may be using this "
                    "node. Refusing to clobber it; coordinate or use a different port."
                )

    def _wait_ready(self, name):
        deadline = time.monotonic() + READY_TIMEOUT
        jsonpath = "jsonpath={.status.conditions[?(@.type=='Ready')].status}"
        while True:
            try:
                out = _kubectl(
                    ["get", "pod", name, "-n", NAMESPACE, "-o", jsonpath],
                    cluster=self._cluster,
                )
            except PortForwardError:
                # The pod vanished between apply and now — e.g. evicted, or
                # deleted by a controller/admission policy. Its events outlive
                # it, so surface them instead of the bare "NotFound".
                raise PortForwardError(
                    f"socat pod {name} disappeared before becoming ready"
                    f"{self._diagnostics(name)}"
                ) from None
            if out.strip() == "True":
                return
            if time.monotonic() >= deadline:
                raise PortForwardError(
                    f"socat pod {name} not ready after {READY_TIMEOUT:g}s"
                    f"{self._diagnostics(name)}"
                )
            time.sleep(POLL_INTERVAL)

    def _wait_reachable(self, host, port):
        """Block until host:port accepts a TCP connection, or time out.

        socat's listener completes the handshake on accept, so a successful
        connect proves the workstation -> service -> pod path is live. (It does
        not prove the pod -> PLC hop; aphyt surfaces any PLC-level error.)
        """
        deadline = time.monotonic() + REACHABLE_TIMEOUT
        while True:
            try:
                with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
                    return
            except OSError as e:
                if time.monotonic() >= deadline:
                    raise PortForwardError(
                        f"{host}:{port} not reachable after {REACHABLE_TIMEOUT:g}s "
                        f"(forward stood up but never became routable): {e}"
                        f"{self._diagnostics(self._name)}"
                    ) from None
                time.sleep(POLL_INTERVAL)

    def _diagnostics(self, name):
        """Best-effort recent events for the pod, to explain a stuck or evicted
        forward. Never raises — diagnosis must not mask the original failure."""
        try:
            events = _kubectl(
                [
                    "get",
                    "events",
                    "-n",
                    NAMESPACE,
                    "--field-selector",
                    f"involvedObject.name={name}",
                    "--sort-by=.lastTimestamp",
                    "-o",
                    "custom-columns=REASON:.reason,MESSAGE:.message",
                    "--no-headers",
                ],
                cluster=self._cluster,
            )
        except PortForwardError:
            return ""
        events = events.strip()
        return f"\nRecent events for {name}:\n{events}" if events else ""

    def down(self):
        """Delete the pod and service. Safe to call when nothing was created."""
        if self._name is None:
            return
        try:
            self._delete(self._name)
        finally:
            self._name = None

    def _delete(self, name):
        # Force-delete the pod (--force --grace-period=0). A graceful delete only
        # sets a deletionTimestamp and leaves the actual removal to the kubelet on
        # the pod's node; when the node name is wrong/non-existent (e.g. a short
        # name instead of the FQDN) no kubelet ever claims the pod, so a graceful
        # delete blocks in Terminating forever and the pod leaks. --force removes
        # it from the API server immediately. On a healthy node this only skips
        # the grace wait — the kubelet still reaps the throwaway socat container.
        _kubectl(
            [
                "delete",
                "pod",
                name,
                "-n",
                NAMESPACE,
                "--ignore-not-found=true",
                "--force",
                "--grace-period=0",
            ],
            cluster=self._cluster,
        )
        _kubectl(
            ["delete", "service", name, "-n", NAMESPACE, "--ignore-not-found=true"],
            cluster=self._cluster,
        )
