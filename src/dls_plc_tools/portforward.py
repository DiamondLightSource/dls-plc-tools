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
import subprocess
import time

# Cluster is always acastus; these mirror pod-pfwd.
NAMESPACE = "accelerator"
IMAGE = "alpine/socat:1.7.4.4-r0"
EIP_PORT = 44818  # EtherNet/IP explicit-messaging port (aphyt's default)
KLOGIN = "/dls_sw/kubernetes/klogin/1.0/acastus"

READY_TIMEOUT = 15.0  # seconds to wait for the socat pod to become Ready
POLL_INTERVAL = 0.5


class PortForwardError(RuntimeError):
    """Raised when a kubectl command fails or the pod never becomes ready."""


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
    """
    cmd = ["bash", "-c", f'source "{KLOGIN}" && exec kubectl "$@"', "bash", *args]
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
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": name}},
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

    def up(self, plc_ip, node, service_ip, port=EIP_PORT):
        """Create the forward ``service_ip:port`` -> ``plc_ip:port`` pinned to
        ``node`` and block until the socat pod is Ready.

        Deletes any stale forward of the same name first, matching pod-pfwd.
        """
        name = name_for(port)
        self._delete(name)  # replace a stale forward rather than clash with it
        manifest = _manifest(name, node, plc_ip, service_ip, port)
        _kubectl(["apply", "-f", "-"], stdin=json.dumps(manifest))
        self._name = name
        self._wait_ready(name)
        return name

    def _wait_ready(self, name):
        deadline = time.monotonic() + READY_TIMEOUT
        jsonpath = "jsonpath={.status.conditions[?(@.type=='Ready')].status}"
        while True:
            out = _kubectl(["get", "pod", name, "-n", NAMESPACE, "-o", jsonpath])
            if out.strip() == "True":
                return
            if time.monotonic() >= deadline:
                raise PortForwardError(
                    f"socat pod {name} not ready after {READY_TIMEOUT:g}s"
                )
            time.sleep(POLL_INTERVAL)

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
