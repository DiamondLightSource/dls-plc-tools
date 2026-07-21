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

# Cluster is always acastus; these mirror pod-pfwd.
NAMESPACE = "accelerator"
IMAGE = "alpine/socat:1.7.4.4-r0"
EIP_PORT = 44818  # EtherNet/IP explicit-messaging port (aphyt's default)
KLOGIN = "/dls_sw/kubernetes/klogin/1.0/acastus"

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


def _kubectl(args, stdin=None, timeout=30):
    """Run one kubectl command with acastus credentials and return its stdout.

    Sources the klogin script (which sets up cluster auth in the shell env) and
    execs kubectl with ``args`` passed as positional parameters, so no user
    input is interpolated into the shell command.

    klogin prints a welcome banner to stdout whenever it (re)authenticates. We
    redirect *its* stdout to /dev/null so that banner can't contaminate the
    captured kubectl output — otherwise parsers like ``_wait_ready``'s exact
    ``"True"`` match break intermittently (only when the banner happens to
    appear). ``exec kubectl`` inherits the shell's original stdout, so kubectl's
    own output is unaffected; klogin's stderr is kept so auth errors still show.
    """
    cmd = [
        "bash",
        "-c",
        f'source "{KLOGIN}" >/dev/null && exec kubectl "$@"',
        "bash",
        *args,
    ]
    result = subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        raise PortForwardError(
            f"kubectl {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout


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

    def __init__(self):
        self._name = None

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

        name = name_for(port)
        report(f"creating forward pod {name} on {node}...")
        self._delete(name)  # replace a stale forward rather than clash with it
        manifest = _manifest(name, node, plc_ip, service_ip, port)
        _kubectl(["apply", "-f", "-"], stdin=json.dumps(manifest))
        self._name = name
        report("waiting for socat pod to be ready...")
        self._wait_ready(name)
        # The pod being Ready isn't enough: aphyt connects to service_ip:port, and
        # the MetalLB/kube-proxy path converges a beat later. Gate on a real TCP
        # connect so the caller doesn't connect into a not-yet-routable IP and
        # tear the whole forward down over a transient timeout.
        report(f"waiting for {service_ip}:{port} to become routable...")
        self._wait_reachable(service_ip, port)
        return name

    def _wait_ready(self, name):
        deadline = time.monotonic() + READY_TIMEOUT
        jsonpath = "jsonpath={.status.conditions[?(@.type=='Ready')].status}"
        while True:
            try:
                out = _kubectl(["get", "pod", name, "-n", NAMESPACE, "-o", jsonpath])
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

    @staticmethod
    def _diagnostics(name):
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
                ]
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

    @staticmethod
    def _delete(name):
        _kubectl(["delete", "pod", name, "-n", NAMESPACE, "--ignore-not-found=true"])
        _kubectl(
            ["delete", "service", name, "-n", NAMESPACE, "--ignore-not-found=true"]
        )
